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
    /// Whether a run the app stopped waiting for has still not ended (`HostCLI.stuckRuns`).
    @Published private(set) var stuck = false

    private let cli: HostCLI
    private var menuObserver: NSObjectProtocol?
    /// How long adding a folder may take. macOS may first ask the user about the folder, and the
    /// progress window's Cancel button stops the run sooner.
    private let addTimeout: TimeInterval = 600

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

    func setAbility(_ ability: Ability, on: Bool) {
        let action = "turn \(on ? "on" : "off") \u{201C}\(ability.title)\u{201D}"
        perform(
            ["ops", on ? "enable" : "disable", ability.name],
            waiting: "Turning \(on ? "on" : "off") \u{201C}\(ability.title)\u{201D}\u{2026}",
            failing: action
        )
    }

    func removeFolder(_ folder: Folder) {
        perform(
            ["roots", "remove", folder.alias],
            waiting: "Removing the folder \u{201C}\(folder.alias)\u{201D}\u{2026}",
            failing: "remove the folder \(folder.alias)"
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
    /// outright; the home folder, or a folder that holds it, is confirmed first. The panel and the
    /// questions are modal, so they start in a `RunLoopTurn` of their own.
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
            let suggestion = Alias.suggest(forFolderName: url.lastPathComponent, taken: taken)
            let name = FileManager.default.displayName(atPath: path)
            let synced = (try? url.resourceValues(forKeys: [.isUbiquitousItemKey]))?.isUbiquitousItem ?? false
            let place = HostText.place(of: path, home: cli.home, synced: synced)
            guard let alias = askForAlias(suggestion: suggestion, folderName: name, taken: taken, place: place) else {
                return
            }
            Task { await addRoot(alias: alias, path: path, confirmedHome: confirmedHome) }
            return
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
        let versionRun = await run(["--version"])
        let opsRun = await run(["ops", "list"])
        let rootsRun = await run(["roots", "list"])
        let results = [versionRun, opsRun, rootsRun]
        version = versionRun.succeeded ? HostText.version(fromVersionOutput: versionRun.stdout) : nil
        abilitiesRead = opsRun.succeeded
        foldersRead = rootsRun.succeeded
        abilities = opsRun.succeeded ? HostText.abilities(fromList: opsRun.stdout) : []
        folders = rootsRun.succeeded ? HostText.folders(fromList: rootsRun.stdout) : []
        problem = results.first(where: { !$0.succeeded }).map { HostText.explain($0) }
        stuck = HostCLI.stuckRuns > 0
        labelStatusItem()
    }

    /// The menu bar icon. Its accessibility description is the menu bar item's title, which
    /// VoiceOver reads: the app's name, and whether Lucille is paused. While something needs the
    /// user, it is a warning sign, so the user sees it without opening the menu.
    static func icon(paused: Bool, attention: Bool = false) -> NSImage {
        let name = HostText.statusItemName(paused: paused)
        let symbol = attention ? "exclamationmark.triangle" : (paused ? "pause.circle" : "desktopcomputer")
        let image = NSImage(systemSymbolName: symbol, accessibilityDescription: name) ?? NSImage()
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
    /// unless one the app gave up on still holds things up.
    private func perform(
        _ verb: [String],
        waiting message: String,
        failing action: String,
        then: @escaping (CLIResult) -> Void = { _ in }
    ) {
        Task {
            let result = await runWithProgress(verb, message: message)
            then(result)
            let stuck = HostCLI.stuckRuns > 0
            if !result.succeeded && (!result.cancelled || stuck || result.status == CLIResult.notFinished) {
                showFailure(HostText.explain(result, earlierRunStuck: stuck), action: action)
            }
            await reload()
        }
    }

    /// Run `roots add`, with a window that has a Cancel button while the host checks the folder,
    /// which can take long on a network drive.
    private func addRoot(alias: String, path: String, confirmedHome: Bool) async {
        let checking = "Checking the folder \u{201C}\(alias)\u{201D}\u{2026}"
        var result = await runWithProgress(
            HostText.addFolderArguments(alias: alias, path: path, confirmedHome: confirmedHome),
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
            guard await RunLoopTurn.ask({ self.confirmBroadFolder(.holdsHome) }) else {
                await reload()
                addFolder()
                return
            }
            result = await runWithProgress(
                HostText.addFolderArguments(alias: alias, path: path, confirmedHome: true),
                message: checking, timeout: addTimeout
            )
        }
        await reload()
        let added = folders.contains(Folder(alias: alias, path: path))
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
            result, during: .addingFolder(HostText.access(of: path)), earlierRunStuck: stuck
        )
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

    /// Ask for the name Lucille will see, starting from a suggestion. Nil when the user cancels.
    private func askForAlias(
        suggestion: String, folderName: String, taken: Set<String>, place: FolderPlace
    ) -> String? {
        var current = suggestion
        var note: String?
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

    private func showFailure(_ explanation: Explanation, action: String) {
        Alerts.shared.show {
            let alert = NSAlert()
            alert.alertStyle = .warning
            alert.messageText = "\(HostText.appName) could not \(action)."
            alert.informativeText = explanation.message
                + (explanation.detail.map { "\n\nDetails: \($0)" } ?? "")
            alert.addButton(withTitle: "OK")
            return alert
        }
    }
}
