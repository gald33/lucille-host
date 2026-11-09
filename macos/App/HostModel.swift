import AppKit
import Foundation

/// What the menu shows of the host's own set-up, and what its items do. Every change goes through
/// the host's command line (`HostCLI`); the app writes none of the host's files. Connecting, the
/// status line and `run` are `Connection`'s, which also says whether the host is paused.
@MainActor
final class HostModel: ObservableObject {
    static let shared = HostModel()

    @Published private(set) var version: String?
    /// Whether the host is paused, as `Connection` last learned it: from `status`, from `run`'s
    /// `state`, or from what `pause`, `resume` and `kill` reported.
    @Published private(set) var paused = false
    @Published private(set) var abilities: [Ability] = []
    @Published private(set) var folders: [Folder] = []
    /// False when `ops list` or `roots list` failed, so the menu does not claim there are none.
    @Published private(set) var abilitiesRead = true
    @Published private(set) var foldersRead = true
    @Published private(set) var problem: Explanation?
    /// Whether a run the app stopped waiting for, of a verb that takes the policy lock, has still
    /// not ended (`HostCLI.stuckRuns`).
    @Published private(set) var stuck = false
    /// Whether the host can open each folder, from `roots check --json`, for Set Up's Folders page:
    /// read while that page shows, and for a folder Set Up adds back.
    @Published private(set) var folderChecks: [FolderCheck] = []
    /// Called once the menu's set-up is read again, once folders are checked, and when the user
    /// says no to turning an ability on: Set Up's window shows what the menu shows.
    var onRead: (() -> Void)?

    private let cli: HostCLI
    private var menuObserver: NSObjectProtocol?
    /// How long adding a folder may take. macOS may first ask the user about the folder, and the
    /// progress window's Cancel button stops the run sooner.
    private let addTimeout: TimeInterval = 600
    /// How long the app waits for the host's suggestion of a folder's alias, which shows no window:
    /// past it, the name dialog starts from the app's own suggestion.
    private let suggestTimeout: TimeInterval = 10

    private init() {
        cli = HostCLI(python: HostCLI.python(inApp: Bundle.main.bundleURL), home: HostCLI.userHome())
        // A copy anywhere but /Applications/<name>.app reads nothing: its menu shows only why it
        // can't run, and Quit (decisions/0002 §13).
        guard Placement.current.isPlaced else {
            return
        }
        // Read the state again each time a menu opens, so a change made in Terminal shows too.
        menuObserver = NotificationCenter.default.addObserver(
            forName: NSMenu.didBeginTrackingNotification, object: nil, queue: .main
        ) { [weak self] _ in
            Task { @MainActor in self?.refresh() }
        }
        refresh()
    }

    var title: String {
        version.map { "\(HostText.appName) \($0)" } ?? HostText.appName
    }

    /// The abilities the menu lists.
    var listedAbilities: [Ability] {
        abilities.filter(\.isShown)
    }

    /// Whether `host.describe` is on: Lucille, once connected, could then see this Mac's set-up.
    var describeIsOn: Bool {
        abilities.first(where: { $0.name == "host.describe" })?.isOn ?? false
    }

    func refresh() {
        Task { await reload() }
    }

    /// Pause or resume, and show what the host then says (`controls`).
    func setPaused(_ pause: Bool) {
        perform(
            ["--json", pause ? "pause" : "resume"],
            waiting: pause ? "Pausing Lucille\u{2026}" : "Resuming Lucille\u{2026}",
            failing: pause ? "pause" : "resume"
        ) { result in
            if let paused = VerbOutcome(result).paused {
                Connection.shared.controlsChanged(paused: paused)
            }
        }
    }

    /// What `Connection` learned of the pause flag.
    func applyPaused(_ value: Bool) {
        guard paused != value else {
            return
        }
        paused = value
        labelStatusItem()
        // SwiftUI may put a new icon in the menu bar after `paused` changes; name that one too.
        Task { @MainActor in
            try? await Task.sleep(for: .milliseconds(500))
            self.labelStatusItem()
        }
    }

    /// The Abilities menu's switch. An ability whose replies can carry a file's contents is turned
    /// on only once the user says so (`Ability.asksFirst`, decided on the host's
    /// `sends_file_content`; spec/CLI.md §6; decisions/0002 §13): the question has Cancel as its
    /// default, and only its second button turns it on. Turning one off, or on one that sends
    /// none, asks nothing.
    func setAbility(_ ability: Ability, on: Bool) {
        guard ability.asksFirst(turningOn: on) else {
            turn(ability, on: on)
            return
        }
        let question = HostText.turnOnQuestion(ability)
        Alerts.shared.show({
            let alert = NSAlert()
            alert.alertStyle = .warning
            alert.messageText = question.title
            alert.informativeText = question.text
            alert.addButton(withTitle: ConnectionText.cancelButton)
            alert.addButton(withTitle: HostText.turnOnButton)
            return alert
        }, done: { [weak self] response in
            if response == .alertSecondButtonReturn {
                self?.turn(ability, on: true)
            } else {
                // Nothing changed: a switch that was clicked shows what the host says again.
                self?.onRead?()
            }
        })
    }

    /// Add back one row Set Up's Add back page had checked (decisions/0002 §11.6): an ordinary
    /// `roots add` or `ops enable`, through the menu's own gates and nothing else. A folder whose
    /// spelling is the whole disk is refused before any run, whatever the set-aside policy marked
    /// it; one that is the home folder or holds it by its path is asked about first, as the menu
    /// asks once the Open panel has given it the folder, and Choose a Smaller Folder opens the Open
    /// panel instead. The folder then goes through `addRoot`, confirmed only by that answer: the
    /// whole disk is refused again, the home folder is asked about once the host refuses it as
    /// `broad_folder`, and the name dialog opens again after `alias_taken`. Then `roots check` on
    /// the folder now held at that path, so that a question macOS asks about it comes now, not at a
    /// request from Lucille. The host's policy holds only folders, so the folder's URL says so, and
    /// nothing reads the disk on the main thread to find out. An ability goes through
    /// `setAbility`, with its acknowledgement for one whose replies can carry a file's contents;
    /// this returns once its switch is turned, as the menu's is, before the host answers.
    func restore(_ item: RestoreItem) async {
        switch item {
        case .folder(let folder):
            guard !HostText.namesWholeDisk(folder.path) else {
                return
            }
            var confirmed = false
            if let kind = HostText.broadFolder(path: folder.path, home: cli.home) {
                guard await RunLoopTurn.ask({ self.confirmBroadFolder(kind) }) else {
                    addFolder()
                    return
                }
                confirmed = true
            }
            await addRoot(alias: folder.alias, url: URL(fileURLWithPath: folder.path, isDirectory: true),
                          place: HostText.place(of: folder.path, home: cli.home), confirmedHome: confirmed)
            guard let held = folders.first(where: { $0.path == folder.path }) else {
                return
            }
            let result = await run(["--json", "roots", "check", held.alias], timeout: SetupFlow.checkLimit)
            folderChecks = folderChecks.filter { $0.alias != held.alias } + VerbOutcome(result).folderChecks
            onRead?()
        case .ability(let ability):
            setAbility(ability, on: true)
        }
    }

    /// `roots check --json`: whether the host can open each folder, for Set Up's Folders page,
    /// with the time adding a folder has, since macOS may first ask the user about a folder.
    func checkFolders() async {
        let result = await run(["--json", "roots", "check"], timeout: SetupFlow.checkLimit)
        folderChecks = VerbOutcome(result).folderChecks
        onRead?()
    }

    /// `ops enable` or `ops disable`, with the window that has a Cancel button if it takes more
    /// than a moment. The switch then follows the host's answer, the operation as it now is, not
    /// what the app asked for (spec/CLI.md §7). Turning off an ability that is already off
    /// (`HostText.alreadyOff`) leaves it as the user asked, so it shows no note.
    private func turn(_ ability: Ability, on: Bool) {
        let action = "turn \(on ? "on" : "off") \u{201C}\(ability.title)\u{201D}"
        perform(
            ["--json", "ops", on ? "enable" : "disable", ability.name],
            waiting: "Turning \(on ? "on" : "off") \u{201C}\(ability.title)\u{201D}\u{2026}",
            failing: action,
            done: on ? [] : HostText.alreadyOff
        ) { [weak self] result in
            self?.answered(VerbOutcome(result))
        }
    }

    /// Set the switch from the host's answer: the operation as `ops enable` or `ops disable` wrote
    /// it once the change was made, or the row of a name this release does not have, which
    /// `ops disable` took out of the policy. The menu then reads its set-up again.
    private func answered(_ outcome: VerbOutcome) {
        if let found = outcome.ability {
            abilities = abilities.map { $0.name == found.name ? found : $0 }
        } else if let gone = outcome.removedOp {
            abilities.removeAll { $0.name == gone }
        }
    }

    /// `roots remove`, by the folder's alias. A folder that is already gone
    /// (`HostText.alreadyRemoved`) is removed, as the user asked, so it shows no note.
    func removeFolder(_ folder: Folder) {
        perform(
            ["--json", "roots", "remove", folder.alias],
            waiting: "Removing the folder \u{201C}\(folder.alias)\u{201D}\u{2026}",
            failing: "remove the folder \(folder.alias)",
            done: HostText.alreadyRemoved
        )
    }

    /// Show the problem the menu reports, with the host's own words; and Reset Settings… first,
    /// when the problem is a settings file the host can't read.
    func showProblem() {
        guard let problem else {
            return
        }
        Alerts.shared.show({
            let alert = NSAlert()
            alert.alertStyle = .warning
            alert.messageText = problem.message
            alert.informativeText = problem.detail.map { "Details: \($0)" } ?? ""
            if problem.offersReset {
                alert.addButton(withTitle: ConnectionText.resetSettingsItem)
            }
            alert.addButton(withTitle: "OK")
            return alert
        }, done: { [weak self] response in
            if problem.offersReset && response == .alertFirstButtonReturn {
                self?.resetSettingsChosen()
            }
        })
    }

    /// Reset Settings…: ask first, with Cancel the default, since a reset turns every folder and
    /// ability away until the user adds them again.
    func resetSettingsChosen() {
        Alerts.shared.show({
            let alert = NSAlert()
            alert.alertStyle = .warning
            alert.messageText = ConnectionText.resetQuestionTitle
            alert.informativeText = ConnectionText.resetQuestionText
            alert.addButton(withTitle: ConnectionText.cancelButton)
            alert.addButton(withTitle: ConnectionText.resetButton)
            return alert
        }, done: { [weak self] response in
            if response == .alertSecondButtonReturn {
                self?.resetSettings()
            }
        })
    }

    /// `policy reset --json`, with the window that has a Cancel button if it takes more than a
    /// moment, then a note that says what came of it. The menu then reads the host's set-up again,
    /// and `Connection` reads `status` again; `run`, if it runs, picks the fresh policy up itself
    /// (spec/CLI.md §13.5). A run the user cancels gets no note, unless the host had already
    /// reset the settings, or one the app gave up on still holds things up.
    private func resetSettings() {
        Task {
            let result = await runWithProgress(["--json", "policy", "reset"], message: ConnectionText.resetting)
            let stuck = HostCLI.stuckRuns > 0
            let outcome = ResetOutcome(result: result)
            await reload()
            Connection.shared.settingsReset()
            if case .failed(.cancelled) = outcome, !stuck {
                return
            }
            let note = ConnectionText.reset(outcome, earlierRunStuck: stuck)
            let retry = outcome.offersTryAgain
            Alerts.shared.show({
                let alert = NSAlert()
                switch outcome {
                case .reset, .nothingToReset:
                    alert.alertStyle = .informational
                case .failed:
                    alert.alertStyle = .warning
                }
                alert.messageText = note.title
                alert.informativeText = note.text + (note.detail.map { "\n\nDetails: \($0)" } ?? "")
                if retry {
                    alert.addButton(withTitle: ConnectionText.tryAgainButton)
                }
                alert.addButton(withTitle: ConnectionText.okButton)
                return alert
            }, done: { [weak self] response in
                if retry && response == .alertFirstButtonReturn {
                    self?.resetSettings()
                }
            })
        }
    }

    /// Choose a folder in the standard Open panel, name it, and add it. The whole disk is refused
    /// outright; the home folder, or a folder that holds it, is confirmed first. The panel, the
    /// questions and the name dialog are modal, so they start in a `RunLoopTurn` of their own.
    func addFolder() {
        RunLoopTurn.perform { [weak self] in
            self?.chooseAndAdd()
        }
    }

    private func chooseAndAdd() {
        var chosen = chooseFolder()
        while let url = chosen {
            let path = url.path
            if HostText.isWholeDisk(path: path) {
                guard refuseWholeDisk() else {
                    return
                }
                chosen = chooseFolder()
                continue
            }
            var confirmedHome = false
            if let kind = HostText.broadFolder(path: path, home: cli.home) {
                guard confirmBroadFolder(kind) else {
                    chosen = chooseFolder()
                    continue
                }
                confirmedHome = true
            }
            let taken = Set(folders.map(\.alias))
            let synced = (try? url.resourceValues(forKeys: [.isUbiquitousItemKey]))?.isUbiquitousItem ?? false
            let place = HostText.place(of: path, home: cli.home, synced: synced)
            Task { await nameAndAdd(url, taken: taken, place: place, confirmedHome: confirmedHome) }
            return
        }
    }

    /// Ask the host for the folder's alias (spec/CLI.md §9.1), off the main thread, with a short
    /// time limit and no window; then show the name dialog, starting from it, in a run loop turn
    /// of its own, and add the folder under the name the user gives. When the host's answer can't
    /// be used, the dialog starts from the app's own suggestion, and nothing is said about it.
    /// `note`, when the app asks again, is the sentence that says why, above the name.
    private func nameAndAdd(
        _ url: URL, taken: Set<String>, place: FolderPlace, confirmedHome: Bool, note: String? = nil
    ) async {
        let path = url.path
        let asked = await run(HostText.suggestAliasArguments(path: path), timeout: suggestTimeout)
        let suggestion = HostText.suggestion(VerbOutcome(asked), folderName: url.lastPathComponent, taken: taken)
        let name = FileManager.default.displayName(atPath: path)
        RunLoopTurn.perform { [weak self] in
            guard let self,
                  let alias = self.askForAlias(
                      suggestion: suggestion, folderName: name, taken: taken, place: place, note: note
                  )
            else {
                return
            }
            Task { await self.addRoot(alias: alias, url: url, place: place, confirmedHome: confirmedHome) }
        }
    }

    private func chooseFolder() -> URL? {
        NSApp.activate()
        let panel = NSOpenPanel()
        panel.title = "Add a Folder"
        panel.message = "Choose a folder Lucille may work in. Abilities you turn on can use the files in it."
        panel.prompt = "Allow \(HostText.appName) to Use This Folder"
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        panel.canCreateDirectories = false
        guard panel.runModal() == .OK else {
            return nil
        }
        return panel.url
    }

    /// Read the version, the abilities and the folders again. Whether the host is paused is
    /// `Connection`'s, from `status`, which opens the Keychain and is not read on every menu.
    private func reload() async {
        let versionRun = await run(["--json", "--version"])
        let opsRun = await run(["--json", "ops", "list"])
        let rootsRun = await run(["--json", "roots", "list"])
        let results = [versionRun, opsRun, rootsRun]
        version = VerbOutcome(versionRun).hostVersion
        abilitiesRead = opsRun.succeeded
        foldersRead = rootsRun.succeeded
        abilities = VerbOutcome(opsRun).abilities
        folders = VerbOutcome(rootsRun).folders
        problem = results.first(where: { !$0.succeeded }).map { HostText.explain($0) }
        stuck = HostCLI.stuckRuns > 0
        labelStatusItem()
        onRead?()
    }

    /// The menu bar icon: the capsule, a template glyph from the app's own Resources
    /// (macos/Icons). While Lucille is paused it has a pause badge; while something needs the user,
    /// an exclamation-mark badge, so the user sees it without opening the menu. There is no glyph
    /// with both: while something needs the user, the exclamation-mark badge takes the pause
    /// badge's place, and the item's name and the menu still say paused. The glyph is a copy, so
    /// that its size and description are not set on the image macOS caches by its name. Without
    /// the files, it is the SF Symbol for the same state. Its accessibility description is the
    /// menu bar item's title, which VoiceOver reads: the app's name, and whether Lucille is paused.
    static func icon(paused: Bool, attention: Bool = false) -> NSImage {
        let name = HostText.statusItemName(paused: paused)
        let glyph = attention ? "MenuBarAttentionTemplate" : (paused ? "MenuBarPausedTemplate" : "MenuBarTemplate")
        let symbol = attention ? "exclamationmark.triangle" : (paused ? "pause.circle" : "desktopcomputer")
        let image: NSImage
        if let found = NSImage(named: glyph)?.copy() as? NSImage {
            found.size = NSSize(width: 18, height: 18)
            image = found
        } else {
            image = NSImage(systemSymbolName: symbol, accessibilityDescription: name) ?? NSImage()
        }
        image.accessibilityDescription = name
        image.isTemplate = true
        return image
    }

    /// Give the menu bar item its name, for VoiceOver, and say there when Lucille is paused: on
    /// the button, and on the icon it shows, whose description macOS reports as the item's title.
    private func labelStatusItem() {
        let name = HostText.statusItemName(paused: paused)
        for window in NSApp.windows {
            var views = window.contentView.map { [$0] } ?? []
            while let view = views.popLast() {
                if let button = view as? NSStatusBarButton {
                    button.image?.accessibilityDescription = name
                    button.setAccessibilityTitle(name)
                    button.setAccessibilityLabel(name)
                    button.toolTip = name
                }
                views.append(contentsOf: view.subviews)
            }
        }
    }

    /// Run a verb on a thread of its own (`VerbQueue`), never on Swift's shared threads.
    private func run(
        _ verb: [String], cancel: CancelToken? = nil, timeout: TimeInterval? = nil
    ) async -> CLIResult {
        await VerbQueue.run(cli, verb, on: VerbQueue.other, cancel: cancel, timeout: timeout)
    }

    /// Run a verb that changes something, with a window that has a Cancel button if it takes more
    /// than a moment, then hand its result to `then`. A run the user cancels is not a failure,
    /// unless one the app gave up on still holds things up; nor is a refusal whose code is in
    /// `done`, which says that things are already as the user asked.
    private func perform(
        _ verb: [String],
        waiting message: String,
        failing action: String,
        done: Set<String> = [],
        then: @escaping (CLIResult) -> Void = { _ in }
    ) {
        Task {
            let result = await runWithProgress(verb, message: message)
            then(result)
            let stuck = HostCLI.stuckRuns > 0
            let alreadyDone = HostText.alreadyDone(result, codes: done)
            if !result.succeeded && !alreadyDone && (!result.cancelled || stuck || result.status == CLIResult.notFinished) {
                showFailure(HostText.explain(result, earlierRunStuck: stuck), action: action)
            }
            await reload()
        }
    }

    /// Run `roots add`, with a window that has a Cancel button while the host checks the folder,
    /// which can take long on a network drive. A refusal is read by its code (`HostText.refusal`):
    /// `broad_folder` asks about the home folder, as the host's `kind` says, unless it is the
    /// whole disk; and `alias_taken`, an alias another verb took after the host suggested it,
    /// opens the name dialog again, with the sentence that says so (spec/CLI.md §7).
    private func addRoot(alias: String, url: URL, place: FolderPlace, confirmedHome: Bool) async {
        let path = url.path
        var confirmed = confirmedHome
        let checking = "Checking the folder \u{201C}\(alias)\u{201D}\u{2026}"
        var result = await runWithProgress(
            HostText.addFolderArguments(alias: alias, path: path, confirmedHome: confirmed),
            message: checking, timeout: addTimeout
        )
        if HostText.needsHomeConfirmation(result) {
            // The host found the folder broad where its path did not show it. The whole disk is
            // still refused outright.
            if HostText.isWholeDisk(path: path) {
                await reload()
                if await RunLoopTurn.ask({ self.refuseWholeDisk() }) {
                    addFolder()
                }
                return
            }
            let kind = HostText.refusedBroadFolder(result)
            guard await RunLoopTurn.ask({ self.confirmBroadFolder(kind) }) else {
                await reload()
                addFolder()
                return
            }
            confirmed = true
            result = await runWithProgress(
                HostText.addFolderArguments(alias: alias, path: path, confirmedHome: true),
                message: checking, timeout: addTimeout
            )
        }
        await reload()
        let added = folders.contains { $0.alias == alias && $0.path == path }
        let stuck = HostCLI.stuckRuns > 0
        if result.cancelled && added {
            showNote(
                title: "The folder was added",
                text: "\(HostText.appName) had already added \u{201C}\(alias)\u{201D} when you cancelled. To undo it, choose Folders, then \(alias), then Remove."
            )
            return
        }
        if result.cancelled && !stuck && result.status != CLIResult.notFinished {
            return
        }
        if result.succeeded {
            let connected = Connection.shared.holdsPair
            showNote(title: "Folder added", text: HostText.folderAdded(alias: alias, connected: connected))
            return
        }
        let explanation = HostText.explain(
            result, during: .addingFolder(HostText.access(of: path), alias: alias), earlierRunStuck: stuck
        )
        if HostText.nameTaken(result) {
            // Another verb took the alias after the host suggested it: ask again, with the
            // sentence that says so above a name no folder has.
            await nameAndAdd(
                url, taken: Set(folders.map(\.alias)).union([alias]), place: place,
                confirmedHome: confirmed, note: explanation.message
            )
            return
        }
        showFailure(explanation, action: "add the folder")
    }

    /// Run a verb; if it takes longer than a moment, show a window with a Cancel button.
    private func runWithProgress(
        _ verb: [String], message: String, timeout: TimeInterval? = nil
    ) async -> CLIResult {
        let token = CancelToken()
        let panel = ProgressPanel(message: message) { token.cancel() }
        let shower = Task { @MainActor in
            try? await Task.sleep(for: .milliseconds(400))
            if !Task.isCancelled {
                panel.show()
            }
        }
        let result = await run(verb, cancel: token, timeout: timeout)
        shower.cancel()
        panel.close()
        return result
    }

    /// Ask for the name Lucille will see, starting from a suggestion, with `first` above it when
    /// the app asks again. Nil when the user cancels.
    private func askForAlias(
        suggestion: String, folderName: String, taken: Set<String>, place: FolderPlace, note first: String? = nil
    ) -> String? {
        var current = suggestion
        var note = first
        while true {
            let alert = NSAlert()
            alert.messageText = "Name Lucille will see"
            alert.informativeText = HostText.aliasPrompt(folderName: folderName, note: note, place: place)
            let field = NSTextField(frame: NSRect(x: 0, y: 0, width: 280, height: 24))
            field.stringValue = current
            field.placeholderString = "Name Lucille will see"
            field.setAccessibilityLabel("Name Lucille will see")
            alert.accessoryView = field
            alert.addButton(withTitle: "Add Folder")
            alert.addButton(withTitle: "Cancel")
            alert.window.initialFirstResponder = field
            NSApp.activate()
            guard alert.runModal() == .alertFirstButtonReturn else {
                return nil
            }
            switch Alias.review(typed: field.stringValue, suggestion: suggestion, taken: taken) {
            case .use(let alias):
                return alias
            case .offer(let alias, let because):
                current = alias
                note = because
            }
        }
    }

    /// Ask before adding a folder that is the home folder or holds it. True when the user chooses
    /// Add Anyway; Choose a Smaller Folder, the default, is false.
    private func confirmBroadFolder(_ kind: BroadFolder) -> Bool {
        let question = HostText.broadFolderQuestion(kind)
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = question.title
        alert.informativeText = question.text
        alert.addButton(withTitle: "Choose a Smaller Folder")
        alert.addButton(withTitle: "Add Anyway")
        NSApp.activate()
        return alert.runModal() == .alertSecondButtonReturn
    }

    /// Say that the app never adds the whole disk. True when the user chooses Choose a Smaller
    /// Folder, the default; false on Cancel. No button adds it.
    private func refuseWholeDisk() -> Bool {
        let refusal = HostText.wholeDiskRefusal(diskName: FileManager.default.displayName(atPath: "/"))
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = refusal.title
        alert.informativeText = refusal.text
        alert.addButton(withTitle: "Choose a Smaller Folder")
        alert.addButton(withTitle: "Cancel")
        NSApp.activate()
        return alert.runModal() == .alertFirstButtonReturn
    }

    /// A note, through `Alerts`, which shows it once no other note is up.
    private func showNote(title: String, text: String) {
        Alerts.shared.show {
            let alert = NSAlert()
            alert.messageText = title
            alert.informativeText = text
            alert.addButton(withTitle: "OK")
            return alert
        }
    }

    /// A run that failed, in a note: its explanation, and, when the explanation offers a place in
    /// macOS, that place's button first (decisions/0002 §20), which opens it. When macOS could not
    /// open it, the note comes again with that said first, and OK alone.
    private func showFailure(_ explanation: Explanation, action: String) {
        let title = "\(HostText.appName) could not \(action)."
        let text = explanation.message + (explanation.detail.map { "\n\nDetails: \($0)" } ?? "")
        let place = explanation.place
        Alerts.shared.show({
            let alert = NSAlert()
            alert.alertStyle = .warning
            alert.messageText = title
            alert.informativeText = text
            if let place {
                alert.addButton(withTitle: PlaceText.button(place))
            }
            alert.addButton(withTitle: "OK")
            return alert
        }, done: { response in
            if let place, response == .alertFirstButtonReturn, !SystemPlaces.open(place) {
                Connection.shared.notOpened(place, title: title, text: text)
            }
        })
    }
}
