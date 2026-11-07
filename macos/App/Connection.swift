import AppKit
import Foundation

/// Runs `body` on the main actor, from a callback the main queue delivers: a dispatch source's
/// handler, a notification observer, or a `HostChild` callback, which may not say that they run
/// there. It stops the app if they ever do not.
func onMain(_ body: @MainActor () -> Void) {
    MainActor.assumeIsolated(body)
}

/// Where the app starts anything that runs a modal loop: a note or question (`runModal`), the Open
/// panel, and its own Quit, whose wait to quit AppKit runs as a modal loop. Each runs on the main
/// thread in a turn of the main run loop of its own, in its common modes, and never inside a
/// main-queue callout: a dispatch source's handler, `DispatchQueue.main.async`, or a main-actor
/// task. The main queue is serial, and the run loop serves it only from outside a callout of it,
/// so a modal loop started inside one would hold back every `MainTimer`, every main-actor task
/// and every child's events until it ended. Started from here, they all go on while it is up.
enum RunLoopTurn {
    static func perform(_ body: @escaping @MainActor () -> Void) {
        RunLoop.main.perform(inModes: [.common]) {
            onMain(body)
        }
        CFRunLoopWakeUp(CFRunLoopGetMain())
    }

    /// `body`'s answer, worked out in a turn of its own: for a question asked from a task.
    @MainActor
    static func ask(_ body: @escaping @MainActor () -> Bool) async -> Bool {
        await withCheckedContinuation { (continuation: CheckedContinuation<Bool, Never>) in
            perform {
                continuation.resume(returning: body())
            }
        }
    }
}

/// The app's children that live, for SIGTERM's handler, which runs on a queue of its own and so
/// cannot read `Connection`'s state.
final class LiveChildren: @unchecked Sendable {
    private let lock = NSLock()
    private var run: HostChild?
    private var pair: HostChild?
    private var closed = false

    /// Hold `child` as the live `run`. False once SIGTERM has come: start nothing then.
    func holdRun(_ child: HostChild) -> Bool {
        lock.lock()
        defer { lock.unlock() }
        guard !closed else {
            return false
        }
        run = child
        return true
    }

    /// Hold `child` as the live `pair`. False once SIGTERM has come: start nothing then.
    func holdPair(_ child: HostChild) -> Bool {
        lock.lock()
        defer { lock.unlock() }
        guard !closed else {
            return false
        }
        pair = child
        return true
    }

    /// `child` has ended, or never started.
    func release(_ child: HostChild) {
        lock.lock()
        if run === child {
            run = nil
        }
        if pair === child {
            pair = nil
        }
        lock.unlock()
    }

    /// SIGTERM came: hold nothing more, and say what lives.
    func close() -> (run: HostChild?, pair: HostChild?) {
        lock.lock()
        defer { lock.unlock() }
        closed = true
        return (run, pair)
    }
}

/// Where the app's short verbs wait for the host: threads of their own, never Swift's shared
/// threads, which a few verbs waiting on a slow hub or a Keychain question would fill.
enum VerbQueue {
    /// The verbs that open the store that keeps the pair, one at a time, so that two Keychain
    /// questions never stack: `status`, `pair status`, `pair confirm`, `pair reject` and `forget`.
    static let store = DispatchQueue(label: "verbs.store", qos: .userInitiated)
    /// Every other short verb.
    static let other = DispatchQueue(label: "verbs.other", qos: .userInitiated, attributes: .concurrent)

    static func run(
        _ cli: HostCLI,
        _ verb: [String],
        on queue: DispatchQueue,
        cancel: CancelToken? = nil,
        timeout: TimeInterval? = nil
    ) async -> CLIResult {
        await withCheckedContinuation { (continuation: CheckedContinuation<CLIResult, Never>) in
            queue.async {
                continuation.resume(returning: cli.run(verb, cancel: cancel, timeout: timeout))
            }
        }
    }
}

/// A one-shot timer on the main queue. The main run loop serves the main queue in every mode, so
/// it fires while a menu is open, and, since every modal loop starts in a `RunLoopTurn`, while a
/// note is up and while the app waits to quit, where a `Timer` in the default mode would not. By
/// default it counts the time the Mac is awake, as `ProcessInfo.systemUptime` does; `wall` counts
/// wall-clock time, which goes on while it sleeps.
@MainActor
final class MainTimer {
    private var source: DispatchSourceTimer?
    private var action: (@MainActor () -> Void)?

    var isScheduled: Bool { source != nil }

    /// Run `action` after `seconds`, in place of anything scheduled before.
    func schedule(after seconds: TimeInterval, wall: Bool = false, _ action: @escaping @MainActor () -> Void) {
        cancel()
        let timer = DispatchSource.makeTimerSource(queue: .main)
        let delay = max(0, seconds)
        if wall {
            timer.schedule(wallDeadline: .now() + delay, leeway: .milliseconds(50))
        } else {
            timer.schedule(deadline: .now() + delay, leeway: .milliseconds(50))
        }
        timer.setEventHandler { [weak self] in
            onMain {
                self?.fire()
            }
        }
        self.action = action
        source = timer
        timer.resume()
    }

    func cancel() {
        source?.cancel()
        source = nil
        action = nil
    }

    private func fire() {
        let run = action
        cancel()
        run?()
    }
}

/// The one place the app's notes and questions about connecting are shown from, one at a time,
/// and only while nothing else is: no note, no Open panel and no menu open. Each is shown with
/// `runModal` from a `RunLoopTurn`, never from inside a step of the pairing flow or the run
/// supervisor, nor inside a main-queue callout, so two modal notes never stack, and while one is
/// up those steps, the timers and the children's events still run.
@MainActor
final class Alerts {
    static let shared = Alerts()

    private struct Request {
        let key: String?
        let build: @MainActor () -> NSAlert
        let done: @MainActor (NSApplication.ModalResponse) -> Void
    }

    private var waiting: [Request] = []
    private var showingKey: String?
    private var showing = false
    private var closed = false
    private var menusOpen = 0
    /// When a note first waited for an open menu. A menu is seldom open for long, so after
    /// `menuPatience` a note is shown anyway: a menu's end that was never announced must not hold
    /// every note back.
    private var waitingForMenuSince: TimeInterval?
    private let menuPatience: TimeInterval = 20
    private let pump: MainTimer
    private var observers: [NSObjectProtocol] = []

    private init() {
        pump = MainTimer()
        let center = NotificationCenter.default
        observers.append(center.addObserver(forName: NSMenu.didBeginTrackingNotification, object: nil, queue: .main) {
            [weak self] _ in
            onMain {
                self?.menusOpen += 1
            }
        })
        observers.append(center.addObserver(forName: NSMenu.didEndTrackingNotification, object: nil, queue: .main) {
            [weak self] _ in
            onMain {
                guard let self else {
                    return
                }
                self.menusOpen = max(0, self.menusOpen - 1)
                self.pumpSoon(0.2)
            }
        })
    }

    /// Show the note `build` makes once nothing else is up, then call `done` with the button
    /// chosen. `key` names a note shown only once at a time; `front` puts it before the rest.
    func show(
        key: String? = nil,
        front: Bool = false,
        _ build: @escaping @MainActor () -> NSAlert,
        done: @escaping @MainActor (NSApplication.ModalResponse) -> Void = { _ in }
    ) {
        guard !closed else {
            return
        }
        if let key, showingKey == key || waiting.contains(where: { $0.key == key }) {
            return
        }
        let request = Request(key: key, build: build, done: done)
        if front {
            waiting.insert(request, at: 0)
        } else {
            waiting.append(request)
        }
        pumpSoon(0)
    }

    /// A note with an OK button.
    func note(_ note: ConnectionNote, warning: Bool = false) {
        show {
            let alert = NSAlert()
            alert.alertStyle = warning ? .warning : .informational
            alert.messageText = note.title
            alert.informativeText = note.text + (note.detail.map { "\n\nDetails: \($0)" } ?? "")
            alert.addButton(withTitle: ConnectionText.okButton)
            return alert
        }
    }

    /// The app is quitting: show nothing more.
    func close() {
        closed = true
        waiting.removeAll()
        pump.cancel()
    }

    private func pumpSoon(_ delay: TimeInterval) {
        guard !closed, !waiting.isEmpty else {
            return
        }
        pump.schedule(after: delay) { [weak self] in
            // The pump is a main-queue callout: the note is shown from a run loop turn instead.
            RunLoopTurn.perform {
                self?.showNext()
            }
        }
    }

    private func showNext() {
        guard !showing, !closed, !waiting.isEmpty else {
            return
        }
        if NSApp.modalWindow != nil {
            pumpSoon(0.5)
            return
        }
        if menusOpen > 0 {
            let now = ProcessInfo.processInfo.systemUptime
            let since = waitingForMenuSince ?? now
            waitingForMenuSince = since
            if now - since < menuPatience {
                pumpSoon(0.5)
                return
            }
            menusOpen = 0
        }
        waitingForMenuSince = nil
        let request = waiting.removeFirst()
        showing = true
        showingKey = request.key
        let alert = request.build()
        NSApp.activate()
        let response = alert.runModal()
        showing = false
        showingKey = nil
        request.done(response)
        pumpSoon(0)
    }
}

/// A window that says what the app waits for, shown only once a verb has taken a few seconds.
@MainActor
final class SlowNotice {
    private let timer: MainTimer
    private var panel: ProgressPanel?

    init(after seconds: TimeInterval, message: String, onCancel: @escaping () -> Void) {
        timer = MainTimer()
        timer.schedule(after: seconds) { [weak self] in
            let shown = ProgressPanel(message: message, onCancel: onCancel)
            shown.show()
            self?.panel = shown
        }
    }

    func end() {
        timer.cancel()
        panel?.close()
        panel = nil
    }
}

/// The Show Details disclosure under a note: a button that shows or hides the helper program's
/// last lines of standard error, in a box that scrolls.
@MainActor
final class DetailsDisclosure: NSView {
    private let toggle: NSButton
    private let box: NSScrollView
    private weak var alert: NSAlert?
    private var open = false
    private let boxWidth: CGFloat = 420
    private let boxHeight: CGFloat = 160

    init(lines: [String], alert: NSAlert) {
        toggle = NSButton(title: ConnectionText.showDetails, target: nil, action: nil)
        box = NSTextView.scrollableTextView()
        self.alert = alert
        super.init(frame: NSRect(x: 0, y: 0, width: 420, height: 30))
        toggle.target = self
        toggle.action = #selector(toggled(_:))
        if let text = box.documentView as? NSTextView {
            text.string = lines.joined(separator: "\n")
            text.isEditable = false
            text.font = .monospacedSystemFont(ofSize: 11, weight: .regular)
        }
        box.hasVerticalScroller = true
        box.isHidden = true
        addSubview(toggle)
        addSubview(box)
        arrange()
    }

    required init?(coder: NSCoder) {
        return nil
    }

    private func arrange() {
        let button = toggle.fittingSize
        let height = open ? button.height + 8 + boxHeight : button.height
        setFrameSize(NSSize(width: boxWidth, height: height))
        toggle.frame = NSRect(x: 0, y: height - button.height, width: button.width, height: button.height)
        box.frame = NSRect(x: 0, y: 0, width: boxWidth, height: boxHeight)
        box.isHidden = !open
    }

    @objc private func toggled(_ sender: Any?) {
        open.toggle()
        toggle.title = open ? ConnectionText.hideDetails : ConnectionText.showDetails
        arrange()
        alert?.layout()
    }
}

/// The app's side of connecting to Lucille and of running the host (decisions/0002 §9): it owns
/// the pairing flow (`PairingFlow`) and the run supervisor (`RunSupervisor`), runs every effect
/// they ask for, feeds back what came of it, and publishes the menu's status line and items.
///
/// - Each step of either commits its state before any effect runs, and an input that arrives
///   while a step runs waits for it. Notes go through `Alerts`; nothing here waits on a note.
/// - Short verbs run on `VerbQueue`, those that open the store one at a time. `status` is read
///   at launch, after each pairing step and verb that changes the pair, and when the menu opens
///   with a reading more than 10 s old, but not while a child of the app runs.
/// - Every timer is a `MainTimer`, and every modal loop starts in a `RunLoopTurn`. The run
///   supervisor gets `ProcessInfo.systemUptime` as its clock, which stops while the Mac sleeps, and
///   the wall clock beside it.
/// - While a child of the app lives, sudden and automatic termination are off, and an activity
///   keeps App Nap from slowing the app.
/// - Only the copy at /Applications/<name>.app launches (`Placement`): any other starts nothing,
///   and only says why.
/// - Quit: the app's own Quit asks first while `run` runs, unless a Disconnect is under way or the
///   user chose Prepare to Update; a quit by Apple event (logout, restart, shutdown, another app)
///   never asks. Either way the app answers `.terminateLater` and replies once `run` has drained
///   (the supervisor's ladder), a Disconnect under way has run `forget`, and `pair` has stopped
///   (SIGINT, then SIGTERM, then SIGKILL).
/// - SIGTERM is handled on a queue of its own (`SignalStop`): the lifeline is closed, `pair` gets
///   SIGINT, then SIGTERM and SIGKILL, and the app exits within a few seconds.
@MainActor
final class Connection: ObservableObject {
    static let shared = Connection()

    /// The pairing items the menu shows.
    struct MenuItems: Equatable {
        var connect = true
        var finish = false
        var stopConnecting = false
        var disconnect = false
        /// Off while a Disconnect, a quit or a pairing verb runs.
        var enabled = true
    }

    /// What a quit waits for before the app replies.
    private struct Ladder {
        var runDone = false
        var pairDone = false
        var replied = false
    }

    private static let runVerb = ["run", "--json-events", "--exit-on-stdin-eof"]
    private static let pairVerb = ["pair", "--code-stdin", "--json"]
    private static let statusVerb = ["--json", "status"]
    /// `status` opens the Keychain, where macOS may first ask the user: two minutes, and after
    /// 3 s a window that says so, with Cancel.
    private static let statusLimit: TimeInterval = 120
    private static let keychainNoticeDelay: TimeInterval = 3
    /// `pair status`, `pair confirm`, `pair reject` and `forget` may wait on the hub, each of whose
    /// calls the host gives up after 40 s.
    private static let hubVerbLimit: TimeInterval = 120
    /// How often the status line is worked out again while nothing else happens.
    private static let healthTick: TimeInterval = 10
    /// How old a `status` reading the menu may show when it opens.
    private static let statusFresh: TimeInterval = 10
    /// How long a pause or resume the app just made counts over what `run` said before it.
    private static let controlsHold: TimeInterval = 10
    /// At a quit, or at Stop Connecting while `pair` waits, after SIGINT, how long `pair` gets
    /// before SIGTERM, then before SIGKILL, then before the app stops waiting for it.
    private static let pairStopWaits: [TimeInterval] = [5, 3, 2]

    @Published private(set) var statusLine = ConnectionText.notConnected
    /// The line under the status line: what needs attention, or why the Mac is not connected.
    @Published private(set) var reason: String?
    @Published private(set) var needsAttention = false
    @Published private(set) var items = MenuItems()
    /// The pair is confirmed: Lucille's requests can reach this Mac.
    @Published private(set) var connected = false
    /// An operation's process runs, and `run` runs: the menu offers Stop What's Running.
    @Published private(set) var operationRunning = false
    /// The update notice's line while one is held, in memory only (decisions/0002 §16): the menu
    /// shows it as an item that shows the notice again, with Prepare to Update….
    @Published private(set) var updateNotice: String?

    private let cli: HostCLI
    private var flow = PairingFlow()
    private var supervisor = RunSupervisor()
    private let panel: PairingPanel
    /// What Lucille asked since the app opened, from `run`'s `request` events, in memory only,
    /// and the window that shows it.
    private var requests = RequestLog()
    private let activityPanel: ActivityPanel
    /// The latest `status --json` that worked, and the host's words when the latest one did not.
    private var status: HostStatus?
    private var statusDetail: String?
    private var lastStatusAt: TimeInterval?
    private var firstStatusRead = false
    private var statusRefreshing = false
    private var statusRefreshAgain = false
    /// A pause or resume the app made, which counts over what `run` said before it.
    private var pausedHold: (value: Bool, at: TimeInterval)?
    private var runChild: HostChild?
    private var pairChild: HostChild?
    /// The last lines `run` wrote on standard error before it last ended.
    private var runTail: [String] = []
    /// The pairing verb that runs now, which the busy page's Cancel stops.
    private var flowToken: CancelToken?
    private var finishing: ProgressPanel?
    private var disconnecting: ProgressPanel?
    private let wake: MainTimer
    private let poll: MainTimer
    private let deadline: MainTimer
    private let childLimit: MainTimer
    private let pairStop: MainTimer
    private var tick: DispatchSourceTimer?
    private var signalSource: DispatchSourceSignal?
    /// The children that live, for SIGTERM's handler.
    private let children = LiveChildren()
    /// `forget` runs: a quit waits for it.
    private var forgetting = false
    /// A Disconnect's drain ran `kill`, which paused the host.
    private var disconnectKilled = false
    private var menuObserver: NSObjectProtocol?
    private var launched = false
    private var welcomeDone = false
    private var afterWelcomeDone = false
    private var userAskedToQuit = false
    /// Prepare to Update was chosen: the quit it starts asks no question (decisions/0002 §16).
    private var skipQuitQuestion = false
    /// This Mac's release, as `run` gave it with Lucille's request for a newer one.
    private var updateHave = ""
    private var ladder: Ladder?
    private var exiting = false
    private var guarded = false
    private var activity: NSObjectProtocol?
    private var stepping = false
    private var pending: [() -> Void] = []

    private init() {
        cli = HostCLI(python: HostCLI.python(inApp: Bundle.main.bundleURL), home: HostCLI.userHome())
        panel = PairingPanel()
        activityPanel = ActivityPanel()
        wake = MainTimer()
        poll = MainTimer()
        deadline = MainTimer()
        childLimit = MainTimer()
        pairStop = MainTimer()
    }

    /// Whether a pair is held here, confirmed or not: folders added now are not set aside when it
    /// is confirmed, as a new pairing sets them aside.
    var holdsPair: Bool {
        if supervisor.runAlive {
            return true
        }
        switch status?.pair {
        case .waiting?, .sasReady?, .sasPending?, .confirmed?:
            return true
        default:
            return false
        }
    }

    // MARK: Launch

    /// At launch: read `status`, and start `run` if the pair is confirmed, paused or not, since
    /// `run` honours the pause flag itself. A copy anywhere but /Applications/<name>.app does
    /// none of it (decisions/0002 §13).
    func launch() {
        guard !launched, Placement.current.isPlaced else {
            return
        }
        launched = true
        signalSource = SignalStop.install(children)
        wirePanel()
        menuObserver = NotificationCenter.default.addObserver(
            forName: NSMenu.didBeginTrackingNotification, object: nil, queue: .main
        ) { [weak self] _ in
            onMain {
                self?.menuOpened()
            }
        }
        let timer = DispatchSource.makeTimerSource(queue: .main)
        timer.schedule(
            deadline: .now() + Connection.healthTick,
            repeating: .seconds(Int(Connection.healthTick)),
            leeway: .seconds(1)
        )
        timer.setEventHandler { [weak self] in
            onMain {
                self?.feedRun(.tick)
            }
        }
        tick = timer
        timer.resume()
        feedRun(.start)
    }

    /// The note that says where the menu is was closed at launch: say that the host is paused if
    /// it is, then pick up a pairing left unfinished. Only Resume resumes.
    func afterWelcome() {
        welcomeDone = true
        afterWelcomeIfReady()
    }

    private func afterWelcomeIfReady() {
        guard welcomeDone, firstStatusRead, !afterWelcomeDone, ladder == nil, !exiting else {
            return
        }
        afterWelcomeDone = true
        guard effectivePaused() else {
            resumePairing()
            return
        }
        Alerts.shared.show(key: "paused", {
            let alert = NSAlert()
            alert.messageText = ConnectionText.pausedTitle
            alert.informativeText = ConnectionText.pausedText
            alert.addButton(withTitle: ConnectionText.resumeButton)
            alert.addButton(withTitle: ConnectionText.notNowButton)
            return alert
        }, done: { [weak self] response in
            if response == .alertFirstButtonReturn {
                self?.feedRun(.userResume)
            }
            self?.resumePairing()
        })
    }

    /// A pairing `status` says is unfinished: the window opens on it, or a note says why it ended.
    private func resumePairing() {
        guard let pair = status?.pair else {
            return
        }
        switch pair {
        case .waiting, .sasReady, .sasPending, .expired, .refused:
            feedFlow(.resume(pair))
        default:
            break
        }
    }

    // MARK: The menu's items

    func connectChosen() {
        if flow.windowOpen {
            panel.bringForward()
            return
        }
        feedFlow(.startConnecting)
    }

    func finishChosen() {
        if flow.windowOpen {
            panel.bringForward()
            return
        }
        guard let pair = status?.pair else {
            return
        }
        feedFlow(.resume(pair))
    }

    func stopConnectingChosen() {
        RunLoopTurn.perform { [weak self] in
            guard let self, self.askStopConnecting() else {
                return
            }
            self.feedFlow(.stopConnecting)
        }
    }

    /// Disconnect: ask, then drain `run` first, then `forget`.
    func disconnectChosen() {
        RunLoopTurn.perform { [weak self] in
            self?.askDisconnect()
        }
    }

    private func askDisconnect() {
        let alert = NSAlert()
        alert.messageText = ConnectionText.disconnectQuestionTitle
        alert.informativeText = ConnectionText.disconnectQuestionText
        alert.addButton(withTitle: ConnectionText.disconnectButton)
        alert.addButton(withTitle: ConnectionText.cancelButton)
        NSApp.activate()
        guard alert.runModal() == .alertFirstButtonReturn else {
            return
        }
        guard ladder == nil, !exiting, disconnecting == nil else {
            return
        }
        let shown = ProgressPanel(message: ConnectionText.disconnecting, buttonTitle: nil)
        shown.show()
        disconnecting = shown
        feedRun(.disconnect)
    }

    /// Stop What's Running, chosen in the menu: the supervisor asks for `kill` only while `run`
    /// runs.
    func stopWhatsRunningChosen() {
        feedRun(.userStop)
    }

    /// Activity…: what Lucille asked since the app opened. The words for each request start from
    /// the core's summary of its operation, read again from `ops list --json` each time it opens.
    func activityChosen() {
        activityPanel.show(requests.entries)
        Task { [weak self] in
            guard let self else {
                return
            }
            let result = await VerbQueue.run(self.cli, ["--json", "ops", "list"], on: VerbQueue.other)
            let outcome = VerbOutcome(result)
            if case .done = outcome {
                self.activityPanel.setSummaries(outcome.summaries)
            } else {
                Connection.log("ops list did not answer for Activity: exit \(result.status)")
            }
        }
    }

    /// The Activity window's Show the Host's Log: its last 200 lines, which the window shows as
    /// the host wrote them. `log` writes nothing when the log is empty (spec/CLI.md §7).
    private func readLog() {
        Task { [weak self] in
            guard let self else {
                return
            }
            let result = await VerbQueue.run(self.cli, ["--json", "log", "--limit", "200"], on: VerbQueue.other)
            let outcome = VerbOutcome(result)
            if case .done = outcome {
                self.activityPanel.showLog(outcome.logLines)
            } else {
                Connection.log("log did not answer: exit \(result.status)")
                self.activityPanel.showLogFailed()
            }
        }
    }

    /// The app's own Quit, from the menu, the note that says where the menu is, the note of a copy
    /// that is not in the Applications folder, or Prepare to Update: the one place the app quits.
    /// AppKit waits to quit in a modal loop, so `terminate` runs in a `RunLoopTurn` of its own: a
    /// note's Quit is chosen inside a turn that shows a note, and the menu's may come inside a
    /// main-queue callout.
    func quitChosen() {
        RunLoopTurn.perform { [weak self] in
            self?.userAskedToQuit = true
            NSApplication.shared.terminate(nil)
            self?.userAskedToQuit = false
            self?.skipQuitQuestion = false
        }
    }

    /// Prepare to Update, from the update notice or the menu (decisions/0002 §16): the app's own
    /// Quit, which lets `run` finish its current task and drain, then quits, with no question
    /// first, so that the Finder can replace the app.
    func prepareToUpdateChosen() {
        skipQuitQuestion = true
        quitChosen()
    }

    /// The update notice: Lucille needs a newer release than this Mac's (decisions/0002 §16). It
    /// shows itself once, when Lucille first asks, and after that only when the menu's line is
    /// chosen: one note under one key, drawn with the highest version Lucille has asked for by
    /// then. Not Now is the default; Prepare to Update quits as the app's own Quit does. There is
    /// no Download button: the notice says in words where to get the newer release.
    func showUpdateNotice() {
        guard updateNotice != nil, ladder == nil, !exiting else {
            return
        }
        Alerts.shared.show(key: "host-too-old", {
            let alert = NSAlert()
            alert.messageText = ConnectionText.hostTooOldTitle(minVersion: self.supervisor.highestMinVersion ?? "")
            alert.informativeText = ConnectionText.hostTooOldText(hostVersion: self.updateHave)
            alert.addButton(withTitle: ConnectionText.notNowButton)
            alert.addButton(withTitle: ConnectionText.prepareToUpdateButton)
            return alert
        }, done: { [weak self] response in
            if response == .alertSecondButtonReturn {
                self?.prepareToUpdateChosen()
            }
        })
    }

    /// The line under Needs attention, or under Not connected, was chosen: say it in a note, with
    /// what the user can do.
    func showAttention() {
        let health = currentHealth()
        guard let text = ConnectionText.reason(health) else {
            return
        }
        var title = ConnectionText.needsAttention
        var body = text
        var buttons = [ConnectionText.okButton]
        var actions: [@MainActor () -> Void] = [{}]
        var details: [String] = []
        switch health {
        case .needsAttention(.halted(let attention)):
            if attention == .keepsStopping {
                title = ConnectionText.keepsStoppingTitle
                body = ConnectionText.keepsStoppingText
            }
            details = detailLines(for: attention)
            if attention.offersTryAgain {
                buttons = [ConnectionText.tryAgainButton, ConnectionText.okButton]
                actions = [{ [weak self] in self?.tryAgain() }, {}]
            } else if attention == .pairUnusable {
                buttons = [ConnectionText.disconnectItem, ConnectionText.okButton]
                actions = [{ [weak self] in self?.disconnectChosen() }, {}]
            }
        case .needsAttention(.policyInvalid):
            // `run` acts on nothing until the file can be read again, which `policy reset` makes so;
            // `run` then goes on by itself (spec/CLI.md §13.5).
            buttons = [ConnectionText.resetSettingsItem, ConnectionText.okButton]
            actions = [{ HostModel.shared.resetSettingsChosen() }, {}]
        case .notConnected:
            title = ConnectionText.notConnected
        default:
            break
        }
        let shownTitle = title
        let shownBody = body
        let shownButtons = buttons
        let shownDetails = details
        let shownActions = actions
        Alerts.shared.show(key: "attention", {
            let alert = NSAlert()
            alert.alertStyle = .warning
            alert.messageText = shownTitle
            alert.informativeText = shownBody
            for button in shownButtons {
                alert.addButton(withTitle: button)
            }
            if !shownDetails.isEmpty {
                alert.accessoryView = DetailsDisclosure(lines: shownDetails, alert: alert)
            }
            return alert
        }, done: { response in
            let index = response.rawValue - NSApplication.ModalResponse.alertFirstButtonReturn.rawValue
            if shownActions.indices.contains(index) {
                shownActions[index]()
            }
        })
    }

    /// Try Again: read `status` again, and start `run` if it can.
    private func tryAgain() {
        if case .halted(.needsAttention) = supervisor.phase {
            feedRun(.tryAgain)
        } else {
            feedRun(.start)
        }
    }

    private func detailLines(for attention: Attention) -> [String] {
        switch attention {
        case .statusFailed, .insecureKeyring, .storeError, .severalPairs, .pairUnusable:
            if let statusDetail, !statusDetail.isEmpty {
                return [statusDetail]
            }
            return runTail
        case .keepsStopping, .anotherCopy, .startupRefused, .downgrade, .appBug, .contract:
            return runTail
        }
    }

    // MARK: Pause and resume

    /// `pause`, `resume` or `kill` said whether the host is paused now.
    func controlsChanged(paused: Bool) {
        pausedHold = (paused, Connection.uptime())
        if var known = status {
            known.paused = paused
            status = known
        }
        publish()
    }

    private func effectivePaused() -> Bool {
        if let hold = pausedHold {
            return hold.value
        }
        return supervisor.paused(status: status)
    }

    // MARK: Quit

    /// applicationShouldTerminate.
    func shouldTerminate() -> NSApplication.TerminateReply {
        let own = userAskedToQuit
        userAskedToQuit = false
        let skip = skipQuitQuestion
        skipQuitQuestion = false
        if exiting {
            return .terminateNow
        }
        let kind: RunSupervisor.DrainKind = own ? .quit : Connection.quitKindOfCurrentEvent()
        if ladder != nil {
            // A quit while the app already waits to quit: a logout shortens the wait.
            if kind == .logout {
                feedRun(.quit(.logout))
            }
            return .terminateLater
        }
        if !supervisor.runAlive && pairChild == nil && !forgetting {
            Alerts.shared.close()
            return .terminateNow
        }
        // While a Disconnect is under way, the Mac stops answering Lucille anyway: no question.
        // Nor after Prepare to Update, whose user chose to quit for the newer release.
        if own && !skip && supervisor.runAlive && disconnecting == nil {
            guard askQuit() else {
                return .terminateCancel
            }
            if !supervisor.runAlive && pairChild == nil && !forgetting {
                Alerts.shared.close()
                return .terminateNow
            }
        }
        beginQuit(kind)
        return .terminateLater
    }

    private func askQuit() -> Bool {
        let alert = NSAlert()
        alert.messageText = ConnectionText.quitQuestionTitle
        alert.informativeText = ConnectionText.quitQuestionText
        alert.addButton(withTitle: ConnectionText.quitButton)
        alert.addButton(withTitle: ConnectionText.cancelButton)
        NSApp.activate()
        return alert.runModal() == .alertFirstButtonReturn
    }

    private func beginQuit(_ kind: RunSupervisor.DrainKind) {
        ladder = Ladder()
        Alerts.shared.close()
        poll.cancel()
        deadline.cancel()
        childLimit.cancel()
        flowToken?.cancel()
        disconnecting?.close()
        disconnecting = nil
        panel.hide()
        if pairChild == nil {
            ladder?.pairDone = true
        } else {
            stopPair(waits: Connection.pairStopWaits)
        }
        feedRun(.quit(kind))
    }

    private func replyIfDone() {
        guard let current = ladder, current.runDone, current.pairDone, !current.replied, !forgetting else {
            return
        }
        ladder?.replied = true
        finishing?.close()
        finishing = nil
        // In a turn of its own: never before applicationShouldTerminate has returned.
        RunLoopTurn.perform {
            NSApp.reply(toApplicationShouldTerminate: true)
        }
    }

    /// Whether the quit Apple event that asked the app to quit is a logout, a restart or a
    /// shutdown, by its `kAEQuitReason` attribute ('why?'). Anything else is a plain quit.
    private static func quitKindOfCurrentEvent() -> RunSupervisor.DrainKind {
        guard let event = NSAppleEventManager.shared().currentAppleEvent,
              let why = event.attributeDescriptor(forKeyword: fourCharCode("why?"))
        else {
            return .quit
        }
        // kAELogOut, kAEReallyLogOut, kAEShowRestartDialog, kAERestart, kAEShowShutdownDialog,
        // kAEShutDown.
        let endsTheSession = ["logo", "rlgo", "rrst", "rest", "rsdn", "shut"].map(fourCharCode)
        return endsTheSession.contains(why.typeCodeValue) ? .logout : .quit
    }

    nonisolated private static func fourCharCode(_ text: String) -> UInt32 {
        text.utf8.reduce(UInt32(0)) { ($0 << 8) | UInt32($1) }
    }

    /// Stop `pair`: SIGINT, which keeps the pair; then SIGTERM and SIGKILL, each after its wait in
    /// `waits`; then stop waiting for it. `pairGone()` runs once it has exited, or then.
    private func stopPair(waits: [TimeInterval]) {
        guard let child = pairChild else {
            pairGone()
            return
        }
        child.interrupt()
        escalate(child, waits: waits[...], step: 0)
    }

    private func escalate(_ child: HostChild, waits: ArraySlice<TimeInterval>, step: Int) {
        guard let wait = waits.first else {
            pairGone()
            return
        }
        pairStop.schedule(after: wait) { [weak self] in
            guard let self, self.pairChild === child else {
                return
            }
            switch step {
            case 0:
                child.terminate()
            case 1:
                child.killNow()
            default:
                break
            }
            self.escalate(child, waits: waits.dropFirst(), step: step + 1)
        }
    }

    private func pairGone() {
        pairStop.cancel()
        if exiting {
            exit(0)
        }
        if ladder != nil {
            ladder?.pairDone = true
            replyIfDone()
        }
    }

    /// SIGTERM came (`SignalStop`, which stops the children and exits): show nothing more, and
    /// start nothing. This runs only if the main queue is free before the app has exited.
    func signalArrived() {
        exiting = true
        Alerts.shared.close()
        flowToken?.cancel()
        panel.hide()
    }

    // MARK: Steps

    /// Run `body`, a step of the flow or of the supervisor, once no other step runs, then publish
    /// what the menu shows.
    private func step(_ body: @escaping () -> Void) {
        pending.append(body)
        guard !stepping else {
            return
        }
        stepping = true
        while !pending.isEmpty {
            pending.removeFirst()()
        }
        stepping = false
        publish()
    }

    private func feedRun(_ input: RunSupervisor.Input, epoch: Int? = nil) {
        step { [self] in
            let effects = supervisor.handle(
                input, epoch: epoch ?? supervisor.epoch, now: Connection.uptime(), wall: Date()
            )
            let issued = supervisor.epoch
            for effect in effects {
                runEffect(effect, epoch: issued)
            }
            scheduleWake()
        }
    }

    private func feedFlow(_ input: PairingFlow.Input, epoch: Int? = nil) {
        step { [self] in
            let wasOpen = flow.windowOpen
            let effects = flow.handle(input, epoch: epoch ?? flow.epoch)
            let issued = flow.epoch
            renderPanel()
            for effect in effects {
                flowEffect(effect, epoch: issued)
            }
            if wasOpen && !flow.windowOpen {
                // The window closed on a pair `status` may not know of yet: the menu's items
                // (Finish Connecting…, Stop Connecting…) come from `status`.
                refreshStatus()
            }
        }
    }

    private func scheduleWake() {
        guard let next = supervisor.nextWake else {
            wake.cancel()
            return
        }
        wake.schedule(after: next - Connection.uptime() + 0.01) { [weak self] in
            self?.feedRun(.tick)
        }
    }

    // MARK: The run supervisor's effects

    private func runEffect(_ effect: RunSupervisor.Effect, epoch: Int) {
        switch effect {
        case .readStatus:
            Task { [weak self] in
                guard let self else {
                    return
                }
                let outcome = await self.readStatus()
                if let found = outcome.status {
                    self.feedRun(.statusRead(found), epoch: epoch)
                } else {
                    self.feedRun(.statusFailed(outcome), epoch: epoch)
                }
            }
        case .spawnRun:
            startRun()
        case .closeLifeline:
            runChild?.closeLifeline()
        case .runKill:
            if disconnecting != nil {
                // A Disconnect's drain reached its bound: `kill` pauses the host, which its note says.
                disconnectKilled = true
            }
            Task { [weak self] in
                guard let self else {
                    return
                }
                let result = await VerbQueue.run(self.cli, ["--json", "kill"], on: VerbQueue.other)
                if let paused = VerbOutcome(result).paused {
                    self.controlsChanged(paused: paused)
                } else {
                    Connection.log("kill did not answer: exit \(result.status)")
                }
            }
        case .sigkillRun:
            runChild?.killNow()
        case .runForget:
            if ladder != nil {
                // A quit came during a Disconnect, which is finished first.
                finishing?.setMessage(ConnectionText.disconnecting)
                finishing?.setButtonEnabled(false)
            }
            forget()
        case .runResume:
            HostModel.shared.setPaused(false)
        case .stopWhatsRunning:
            stopWhatsRunning()
        case .replyTerminate:
            if ladder != nil {
                ladder?.runDone = true
                replyIfDone()
            }
        case .terminationGuard:
            // Worked out from the children that live (`guardTermination`), which counts `pair`
            // too.
            break
        case .showFinishing(let logout):
            showFinishing(logout: logout)
        case .finishingStopping:
            finishing?.setMessage(ConnectionText.stoppingNow)
            finishing?.setButtonEnabled(false)
        case .hideFinishing:
            finishing?.close()
            finishing = nil
        case .tellHostTooOld(let least, let have):
            // The supervisor tells this once for each higher version, and the notice shows itself
            // only the first time: the user is told once, however many requests say so (spec/CLI.md
            // §6, `host_too_old`). Later tellings only move the menu's line to the highest version;
            // the line shows the notice again, which reads that version when it is drawn.
            let first = updateNotice == nil
            updateNotice = ConnectionText.hostTooOldTitle(minVersion: supervisor.highestMinVersion ?? least)
            updateHave = have
            if first {
                showUpdateNotice()
            }
        case .log(let line):
            Connection.log(line)
        }
    }

    /// Stop What's Running: `kill --json`, which stops the process group of the operation in
    /// flight and pauses the host (spec/CLI.md §7, spec/PROTOCOL.md §16). The menu then says
    /// paused, and a note says so. Nothing here resumes: only the user's Resume does.
    private func stopWhatsRunning() {
        Task { [weak self] in
            guard let self else {
                return
            }
            let result = await VerbQueue.run(self.cli, ["--json", "kill"], on: VerbQueue.other)
            let outcome = VerbOutcome(result)
            if let paused = outcome.paused {
                self.controlsChanged(paused: paused)
                Alerts.shared.note(
                    ConnectionNote(title: ConnectionText.stoppedAndPausedTitle, text: ConnectionText.stoppedAndPausedText, detail: nil)
                )
                return
            }
            Connection.log("kill did not answer: exit \(result.status)")
            var text = ConnectionText.stopWhatsRunningUnknown
            var detail: String?
            if case .refused(let error) = outcome {
                text = ConnectionText.refusal(code: error.code, message: error.message, afterSAS: false)
                detail = error.message == text ? nil : error.message
            }
            Alerts.shared.note(
                ConnectionNote(title: ConnectionText.couldNotStopWhatsRunning, text: text, detail: detail), warning: true
            )
        }
    }

    private func startRun() {
        let child = HostChild(cli: cli, verb: Connection.runVerb, input: .lifeline, deliverOn: .main)
        child.onObject = { [weak self, weak child] object in
            onMain {
                guard let self, let child, child === self.runChild else {
                    return
                }
                if case .state(let state) = object, let hold = self.pausedHold, hold.value == state.paused {
                    self.pausedHold = nil
                }
                if case .request(let event) = object {
                    self.requests.note(event, at: Date())
                    self.activityPanel.update(self.requests.entries)
                }
                self.feedRun(.object(object))
            }
        }
        child.onUnusableLine = { note in
            Connection.log("run wrote \(note)")
        }
        child.onExit = { [weak self, weak child] childExit in
            onMain {
                guard let self, let child, child === self.runChild else {
                    return
                }
                self.children.release(child)
                self.runTail = child.stderrTail
                self.runChild = nil
                self.requests.runEnded(at: Date())
                self.activityPanel.update(self.requests.entries)
                self.guardTermination()
                self.feedRun(.exited(childExit))
            }
        }
        guard children.holdRun(child) else {
            // SIGTERM came: the app is exiting.
            return
        }
        runChild = child
        do {
            try child.start()
        } catch {
            children.release(child)
            runChild = nil
            Connection.log("run could not be started: \(error)")
            DispatchQueue.main.async { [weak self] in
                onMain {
                    self?.feedRun(.spawnFailed)
                }
            }
        }
        guardTermination()
    }

    private func showFinishing(logout: Bool) {
        finishing?.close()
        let shown = ProgressPanel(
            message: ConnectionText.finishing,
            detail: logout ? ConnectionText.finishingLogout : nil,
            buttonTitle: ConnectionText.stopNow
        ) { [weak self] in
            self?.feedRun(.stopNow)
        }
        shown.show()
        finishing = shown
    }

    /// `forget --json`, once a Disconnect has drained `run`. If the pair is still there after it,
    /// `run` is started again, as for any confirmed pair.
    private func forget() {
        forgetting = true
        let paused = disconnectKilled
        disconnectKilled = false
        Task { [weak self] in
            guard let self else {
                return
            }
            let result = await VerbQueue.run(
                self.cli, ["--json", "forget"], on: VerbQueue.store, timeout: Connection.hubVerbLimit
            )
            self.forgetting = false
            let outcome = VerbOutcome(result)
            self.disconnecting?.close()
            self.disconnecting = nil
            if self.ladder != nil || self.exiting {
                // A quit came during the Disconnect: nothing more is shown or started, and the
                // app may now reply.
                Connection.log("forget at quit exited \(result.status)")
                self.replyIfDone()
                return
            }
            var done = false
            switch outcome {
            case .done:
                done = outcome.forget != nil
            case .refused(let error):
                done = error.code == "none"
            default:
                done = false
            }
            var note = ConnectionText.disconnected(outcome)
            if paused {
                note = ConnectionNote(title: note.title, text: note.text + " " + ConnectionText.pausedByDisconnect, detail: note.detail)
            }
            Alerts.shared.note(note, warning: !done)
            if done {
                self.refreshStatus()
            } else {
                self.feedRun(.start)
            }
        }
    }

    // MARK: The pairing flow's effects

    private func flowEffect(_ effect: PairingFlow.Effect, epoch: Int) {
        if ladder != nil || exiting {
            // Quitting: stop what runs, and start nothing.
            switch effect {
            case .interruptPair:
                pairChild?.interrupt()
            case .cancelPoll:
                poll.cancel()
            case .cancelDeadline:
                deadline.cancel()
            case .clearCodeField:
                panel.clearCode()
            default:
                break
            }
            return
        }
        switch effect {
        case .startPair(let code):
            startPair(code: code, epoch: epoch)
        case .interruptPair:
            if flow.screen == .busy(.rejecting) {
                // Stop Connecting: `pair reject` waits for this child to end, so it does not wait
                // on SIGINT alone. SIGTERM and SIGKILL follow, as at a quit.
                stopPair(waits: Connection.pairStopWaits)
            } else {
                pairChild?.interrupt()
            }
        case .run(let verb):
            runFlowVerb(verb, epoch: epoch)
        case .pollAfter(let seconds):
            poll.schedule(after: seconds) { [weak self] in
                self?.feedFlow(.pollDue, epoch: epoch)
            }
        case .cancelPoll:
            poll.cancel()
        case .deadlineAt(let when):
            deadline.schedule(after: when.timeIntervalSinceNow, wall: true) { [weak self] in
                self?.feedFlow(.deadlinePassed(at: Date()), epoch: epoch)
            }
        case .cancelDeadline:
            deadline.cancel()
        case .clearCodeField:
            panel.clearCode()
        case .say(let note):
            Alerts.shared.note(ConnectionText.note(note), warning: Connection.isWarning(note))
        case .refreshStatus:
            refreshStatus()
        case .startRunCheck:
            if case .halted(.needsAttention) = supervisor.phase {
                feedRun(.tryAgain)
            } else {
                feedRun(.start)
            }
        case .cancelRunRetries:
            feedRun(.pairingStarted)
        }
    }

    private static func isWarning(_ note: PairingNote) -> Bool {
        switch note {
        case .confirmed, .rejected:
            return false
        case .refused, .confirmedAnother, .notConfirmed, .confirmUnknown, .rejectFailed, .notStarted:
            return true
        }
    }

    /// Start `pair --code-stdin --json` with the code on its standard input, which nothing logs.
    private func startPair(code: String, epoch: Int) {
        let child = HostChild(cli: cli, verb: Connection.pairVerb, input: .code(code), deliverOn: .main)
        child.onObject = { [weak self, weak child] object in
            onMain {
                guard let self, let child, child === self.pairChild else {
                    return
                }
                self.feedFlow(.childObject(object), epoch: epoch)
            }
        }
        child.onUnusableLine = { note in
            Connection.log("pair wrote \(note)")
        }
        child.onExit = { [weak self, weak child] childExit in
            onMain {
                guard let self, let child, child === self.pairChild else {
                    return
                }
                self.children.release(child)
                self.pairChild = nil
                self.childLimit.cancel()
                self.guardTermination()
                Connection.log(Connection.describePair(childExit.end))
                self.feedFlow(.childExited(childExit.end), epoch: epoch)
                if self.exiting || self.ladder != nil {
                    self.pairGone()
                }
            }
        }
        guard children.holdPair(child) else {
            // SIGTERM came: the app is exiting.
            return
        }
        pairChild = child
        do {
            try child.start()
        } catch {
            children.release(child)
            pairChild = nil
            Connection.log("pair could not be started: \(error)")
            DispatchQueue.main.async { [weak self] in
                onMain {
                    self?.feedFlow(.childNotStarted, epoch: epoch)
                }
            }
            guardTermination()
            return
        }
        childLimit.schedule(after: Pairing.childTimeLimit) { [weak self] in
            self?.feedFlow(.childTimeLimit, epoch: epoch)
        }
        guardTermination()
    }

    nonisolated private static func describePair(_ end: ChildEnd) -> String {
        switch end {
        case .exited(let code):
            return "pair exited \(code)"
        case .signaled(let number):
            return "pair ended by signal \(number)"
        }
    }

    private func runFlowVerb(_ verb: PairingFlow.Verb, epoch: Int) {
        let token = CancelToken()
        flowToken = token
        Task { [weak self] in
            guard let self else {
                return
            }
            let outcome: VerbOutcome
            switch verb {
            case .status:
                outcome = await self.readStatus(cancel: token)
            case .pairStatus:
                outcome = await self.hubVerb(["--json", "pair", "status"], cancel: token)
            case .pairConfirm:
                outcome = await self.hubVerb(["--json", "pair", "confirm"], cancel: token)
            case .pairReject:
                outcome = await self.hubVerb(["--json", "pair", "reject"], cancel: token)
            }
            if self.flowToken === token {
                self.flowToken = nil
            }
            self.feedFlow(.verbResult(verb, outcome), epoch: epoch)
        }
    }

    private func hubVerb(_ verb: [String], cancel: CancelToken) async -> VerbOutcome {
        let result = await VerbQueue.run(
            cli, verb, on: VerbQueue.store, cancel: cancel, timeout: Connection.hubVerbLimit
        )
        return VerbOutcome(result)
    }

    private func wirePanel() {
        activityPanel.onShowLog = { [weak self] in
            self?.readLog()
        }
        panel.onSubmit = { [weak self] text in
            self?.feedFlow(.submit(text))
        }
        panel.onClose = { [weak self] in
            self?.feedFlow(.windowClosed)
        }
        panel.onStopAsked = { [weak self] in
            self?.stopConnectingChosen()
        }
        panel.onCancelBusy = { [weak self] in
            self?.flowToken?.cancel()
        }
        panel.onDrawn = { [weak self] sas, at in
            self?.feedFlow(.sasDrawn(sas, at: at))
        }
        panel.onMatch = { [weak self] at in
            self?.feedFlow(.theyMatch(at: at))
        }
        panel.onDiffer = { [weak self] in
            self?.feedFlow(.theyDiffer)
        }
    }

    private func askStopConnecting() -> Bool {
        let alert = NSAlert()
        alert.messageText = ConnectionText.stopQuestionTitle
        alert.informativeText = ConnectionText.stopQuestionText
        alert.addButton(withTitle: ConnectionText.stopConnectingButton)
        alert.addButton(withTitle: ConnectionText.keepWaitingButton)
        NSApp.activate()
        return alert.runModal() == .alertFirstButtonReturn
    }

    private func renderPanel() {
        switch flow.screen {
        case .closed:
            panel.hide()
        case .enterCode:
            panel.showCode(setAside: settingsWouldBeSetAside())
        case .waiting(let skewed):
            panel.showWaiting(clockSkewed: skewed)
        case .compare(let comparison):
            panel.showCompare(comparison)
        case .busy(let busy):
            panel.showBusy(busy)
        }
    }

    /// Whether connecting would set something aside: a folder, or an ability other than
    /// `host.describe`, which a new pairing's policy has on. A name the policy holds that this
    /// release does not have is set aside too, though the host says it is not on.
    private func settingsWouldBeSetAside() -> Bool {
        let model = HostModel.shared
        return !model.folders.isEmpty
            || model.abilities.contains { ($0.isOn || $0.state == .notInCatalog) && $0.name != "host.describe" }
    }

    // MARK: status

    /// Read `status --json`, keep what it says, and return it. After a few seconds a window says
    /// that macOS may be asking about the Keychain, with Cancel.
    private func readStatus(cancel token: CancelToken = CancelToken()) async -> VerbOutcome {
        let started = Connection.uptime()
        let notice = SlowNotice(after: Connection.keychainNoticeDelay, message: ConnectionText.keychainWait) {
            token.cancel()
        }
        let result = await VerbQueue.run(
            cli, Connection.statusVerb, on: VerbQueue.store, cancel: token, timeout: Connection.statusLimit
        )
        notice.end()
        let outcome = VerbOutcome(result)
        lastStatusAt = Connection.uptime()
        if let found = outcome.status {
            status = found
            statusDetail = nil
            if let hold = pausedHold, started >= hold.at {
                pausedHold = nil
            }
        } else if case .refused(let error) = outcome {
            statusDetail = error.message
        } else {
            let words = HostText.reasons(result.stderr)
            statusDetail = words.isEmpty ? nil : words
        }
        if !firstStatusRead {
            firstStatusRead = true
            afterWelcomeIfReady()
        }
        publish()
        return outcome
    }

    /// Read `status` again for the menu, at most one reading at a time.
    private func refreshStatus() {
        guard ladder == nil, !exiting else {
            return
        }
        if statusRefreshing {
            statusRefreshAgain = true
            return
        }
        statusRefreshing = true
        Task { [weak self] in
            guard let self else {
                return
            }
            _ = await self.readStatus()
            self.statusRefreshing = false
            if self.statusRefreshAgain {
                self.statusRefreshAgain = false
                self.refreshStatus()
            }
        }
    }

    /// Reset Settings has run: read `status` again, unless a child of the app runs, whose events
    /// say more. `run` picks the fresh policy up itself, with no restart (spec/CLI.md §13.5).
    func settingsReset() {
        guard ladder == nil, !exiting, runChild == nil, pairChild == nil else {
            return
        }
        refreshStatus()
    }

    /// The menu opened: show what is known, and read `status` again if it is old and no child of
    /// the app runs, whose events, or whose verbs, say more.
    private func menuOpened() {
        publish()
        guard ladder == nil, !exiting, runChild == nil, pairChild == nil, !statusRefreshing else {
            return
        }
        if let last = lastStatusAt, Connection.uptime() - last < Connection.statusFresh {
            return
        }
        refreshStatus()
    }

    // MARK: What the menu shows

    private func currentHealth() -> Health {
        supervisor.health(now: Connection.uptime(), wall: Date(), status: status)
    }

    private func publish() {
        if let hold = pausedHold, Connection.uptime() - hold.at > Connection.controlsHold {
            pausedHold = nil
        }
        let health = currentHealth()
        let paused = effectivePaused()
        let line = ConnectionText.statusLine(health, paused: paused)
        if statusLine != line {
            statusLine = line
        }
        let why = ConnectionText.reason(health)
        if reason != why {
            reason = why
        }
        var attention = false
        if case .needsAttention = health {
            attention = true
        }
        if needsAttention != attention {
            needsAttention = attention
        }
        let confirmed = isConfirmed()
        if connected != confirmed {
            connected = confirmed
        }
        // Not while `run` drains: the drain's own ladder stops the operation, and the supervisor
        // asks for nothing then.
        var draining = false
        if case .draining = supervisor.phase {
            draining = true
        }
        if operationRunning != (supervisor.runAlive && requests.operationRunning && !draining) {
            operationRunning = supervisor.runAlive && requests.operationRunning && !draining
        }
        let shown = menuItems()
        if items != shown {
            items = shown
        }
        HostModel.shared.applyPaused(paused)
    }

    private func isConfirmed() -> Bool {
        if case .running(let run) = supervisor.phase, let state = run.state {
            return state.confirmed
        }
        if case .confirmed? = status?.pair {
            return true
        }
        return false
    }

    private func menuItems() -> MenuItems {
        var shown = MenuItems()
        shown.enabled = ladder == nil && !exiting && disconnecting == nil && !flow.isBusy
        if supervisor.runAlive {
            shown.connect = false
            shown.disconnect = true
            return shown
        }
        switch status?.pair {
        case nil, .noPair?, .other?:
            break
        case .waiting?, .sasReady?, .sasPending?:
            shown.connect = false
            shown.finish = !flow.windowOpen
            shown.stopConnecting = true
        case .expired?, .refused?:
            shown.connect = false
            shown.stopConnecting = true
        case .confirmed?:
            shown.connect = false
            shown.disconnect = true
        case .severalPairs?:
            // Neither `forget` nor `pair reject` can end it: the line under the status line says
            // what to do.
            shown.connect = false
        case .unreadable(let code)?:
            shown.connect = false
            shown.disconnect = code == "pair_unusable"
        }
        if flow.windowOpen || pairChild != nil {
            // The window is open on a pairing that `status` may not show yet: the item brings the
            // window back.
            shown.connect = !shown.finish && !shown.stopConnecting && !shown.disconnect
        }
        return shown
    }

    // MARK: Termination

    /// Sudden and automatic termination stay off, and an activity holds App Nap off, while a child
    /// of the app lives, whose output it must keep reading.
    private func guardTermination() {
        let needed = runChild != nil || pairChild != nil
        guard needed != guarded else {
            return
        }
        guarded = needed
        let info = ProcessInfo.processInfo
        let why = "Lucille's helper program is running"
        if needed {
            info.disableSuddenTermination()
            info.disableAutomaticTermination(why)
            activity = info.beginActivity(options: [.userInitiatedAllowingIdleSystemSleep], reason: why)
        } else {
            if let activity {
                info.endActivity(activity)
            }
            activity = nil
            info.enableAutomaticTermination(why)
            info.enableSuddenTermination()
        }
    }

    // MARK: Helpers

    nonisolated private static func uptime() -> TimeInterval {
        ProcessInfo.processInfo.systemUptime
    }

    /// One line on standard error, for diagnostics. It never holds a code or a SAS.
    nonisolated static func log(_ line: String) {
        fputs(line + "\n", stderr)
    }
}

/// SIGTERM to the app (decisions/0002 §9), handled on a queue of its own, never on the main queue,
/// which a note, or AppKit's wait to quit, can hold: `run`'s lifeline is closed, so that it drains
/// on its own; `pair` gets SIGINT, which keeps the pair, then SIGTERM and SIGKILL; and the app
/// exits within a few seconds, asking nothing and running no `kill`.
enum SignalStop {
    /// After SIGINT, how long `pair` gets before SIGTERM, then before SIGKILL, then before the app
    /// stops waiting for it.
    static let pairWaits: [TimeInterval] = [3, 1, 1]
    /// How long the main thread gets to exit before this queue exits instead.
    static let mainWait: TimeInterval = 2
    private static let queue = DispatchQueue(label: "signals", qos: .userInitiated)

    /// Handle SIGTERM from now on. The source must be kept.
    static func install(_ children: LiveChildren) -> DispatchSourceSignal {
        _ = signal(SIGTERM, ignoreSignal)
        let source = DispatchSource.makeSignalSource(signal: SIGTERM, queue: queue)
        source.setEventHandler {
            stop(children)
        }
        source.resume()
        return source
    }

    private static func stop(_ children: LiveChildren) {
        let live = children.close()
        live.run?.closeLifeline()
        DispatchQueue.main.async {
            onMain {
                Connection.shared.signalArrived()
            }
        }
        if let pair = live.pair {
            pair.interrupt()
            if !ended(pair, within: pairWaits[0]) {
                pair.terminate()
                if !ended(pair, within: pairWaits[1]) {
                    pair.killNow()
                    _ = ended(pair, within: pairWaits[2])
                }
            }
        }
        // On the main thread, as AppKit expects, once it takes a turn; from here if it does not.
        RunLoopTurn.perform {
            exit(0)
        }
        Thread.sleep(forTimeInterval: mainWait)
        exit(0)
    }

    /// Whether `child` has started and ended, asked every tenth of a second for `seconds`.
    private static func ended(_ child: HostChild, within seconds: TimeInterval) -> Bool {
        let until = ProcessInfo.processInfo.systemUptime + seconds
        while ProcessInfo.processInfo.systemUptime < until {
            if child.pid != 0 && !child.isRunning {
                return true
            }
            Thread.sleep(forTimeInterval: 0.1)
        }
        return child.pid != 0 && !child.isRunning
    }
}

/// SIGTERM's handler while the app runs: it does nothing, and `SignalStop`'s source does the work.
/// A handler, unlike ignoring the signal, does not pass on to the host's processes, which start
/// with SIGTERM's default action.
private func ignoreSignal(_ number: Int32) {}
