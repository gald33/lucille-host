import AppKit
import Foundation

/// What the menu shows, and what its items do. Every change goes through the host's command line
/// (`HostCLI`); the app writes none of the host's files.
@MainActor
final class HostModel: ObservableObject {
    @Published private(set) var version: String?
    @Published private(set) var paused = false
    @Published private(set) var abilities: [Ability] = []
    @Published private(set) var folders: [Folder] = []
    /// False when `ops list` or `roots list` failed, so the menu does not claim there are none.
    @Published private(set) var abilitiesRead = true
    @Published private(set) var foldersRead = true
    @Published private(set) var problem: Explanation?

    private let cli: HostCLI
    private var menuObserver: NSObjectProtocol?

    init() {
        cli = HostCLI(python: HostCLI.python(inApp: Bundle.main.bundleURL), home: HostCLI.userHome())
        // Read the state again each time a menu opens, so a change made in Terminal shows too.
        menuObserver = NotificationCenter.default.addObserver(
            forName: NSMenu.didBeginTrackingNotification, object: nil, queue: .main
        ) { [weak self] _ in
            Task { @MainActor in self?.refresh() }
        }
        refresh()
    }

    var title: String {
        version.map { "Lucille Host \($0)" } ?? "Lucille Host"
    }

    /// Pairing is not built yet, so this Mac is never connected; the line says so first.
    var statusLine: String {
        paused ? "Not connected to Lucille \u{00B7} Paused" : "Not connected to Lucille"
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

    func setPaused(_ pause: Bool) {
        perform([pause ? "pause" : "resume"], failing: pause ? "pause" : "resume")
    }

    func setAbility(_ ability: Ability, on: Bool) {
        perform(
            ["ops", on ? "enable" : "disable", ability.name],
            failing: "turn \(on ? "on" : "off") \u{201C}\(ability.title)\u{201D}"
        )
    }

    func removeFolder(_ folder: Folder) {
        perform(["roots", "remove", folder.alias], failing: "remove the folder \(folder.alias)")
    }

    /// Show the problem the menu reports, with the host's own words.
    func showProblem() {
        guard let problem else {
            return
        }
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = problem.message
        alert.informativeText = problem.detail.map { "Details: \($0)" } ?? ""
        alert.addButton(withTitle: "OK")
        NSApp.activate()
        alert.runModal()
    }

    /// Choose a folder in the standard Open panel, name it, and add it. A folder the host adds
    /// only with the user's confirmation is confirmed first.
    func addFolder() {
        var chosen = chooseFolder()
        while let url = chosen {
            let path = url.path
            var allowBroad = false
            if let kind = HostText.broadFolder(path: path, home: cli.home) {
                guard confirmBroadFolder(kind) else {
                    chosen = chooseFolder()
                    continue
                }
                allowBroad = true
            }
            let taken = Set(folders.map(\.alias))
            let suggestion = Alias.suggest(forFolderName: url.lastPathComponent, taken: taken)
            let name = FileManager.default.displayName(atPath: path)
            guard let alias = askForAlias(suggestion: suggestion, folderName: name, taken: taken) else {
                return
            }
            Task { await addRoot(alias: alias, path: path, allowBroad: allowBroad) }
            return
        }
    }

    private func chooseFolder() -> URL? {
        NSApp.activate()
        let panel = NSOpenPanel()
        panel.title = "Add a Folder"
        panel.message = "Choose a folder Lucille may work in. Abilities you turn on can use the files in it."
        panel.prompt = "Choose"
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        panel.canCreateDirectories = false
        guard panel.runModal() == .OK else {
            return nil
        }
        return panel.url
    }

    private func reload() async {
        let cli = self.cli
        let results = await Task.detached(priority: .userInitiated) {
            [cli.run(["--version"]), cli.run(["ops", "list"]), cli.run(["roots", "list"])]
        }.value
        let versionRun = results[0]
        let opsRun = results[1]
        let rootsRun = results[2]
        version = versionRun.succeeded ? HostText.version(fromVersionOutput: versionRun.stdout) : nil
        abilitiesRead = opsRun.succeeded
        foldersRead = rootsRun.succeeded
        abilities = opsRun.succeeded ? HostText.abilities(fromList: opsRun.stdout) : []
        folders = rootsRun.succeeded ? HostText.folders(fromList: rootsRun.stdout) : []
        problem = results.first(where: { !$0.succeeded }).map { HostText.explain($0) }
        paused = cli.isPaused
    }

    private func run(_ verb: [String], cancel: CancelToken? = nil) async -> CLIResult {
        let cli = self.cli
        return await Task.detached(priority: .userInitiated) { cli.run(verb, cancel: cancel) }.value
    }

    private func perform(_ verb: [String], failing action: String) {
        Task {
            let result = await run(verb)
            if !result.succeeded {
                showFailure(HostText.explain(result), action: action)
            }
            await reload()
        }
    }

    /// Run `roots add`, with a window that has a Cancel button while the host checks the folder,
    /// which can take long on a network drive.
    private func addRoot(alias: String, path: String, allowBroad: Bool) async {
        var verb = ["roots", "add", alias, path]
        if allowBroad {
            verb.append("--allow-home-or-root")
        }
        var result = await runWithProgress(verb, message: "Checking the folder \u{201C}\(alias)\u{201D}\u{2026}")
        if HostText.needsHomeConfirmation(result) {
            guard confirmBroadFolder(.holdsHome) else {
                await reload()
                addFolder()
                return
            }
            verb.append("--allow-home-or-root")
            result = await runWithProgress(verb, message: "Checking the folder \u{201C}\(alias)\u{201D}\u{2026}")
        }
        await reload()
        let added = folders.contains(Folder(alias: alias, path: path))
        if result.cancelled {
            if added {
                showNote(
                    title: "The folder was added",
                    text: "Lucille Host had already added \u{201C}\(alias)\u{201D} when you cancelled. To undo it, choose Folders, then \(alias), then Remove."
                )
            }
            return
        }
        if result.succeeded {
            showNote(
                title: "Folder added",
                text: "Lucille can now use the folder \u{201C}\(alias)\u{201D}, for abilities you turn on."
            )
            return
        }
        showFailure(HostText.explain(result, during: .addingFolder), action: "add the folder")
    }

    /// Run a verb; if it takes longer than a moment, show a window with a Cancel button.
    private func runWithProgress(_ verb: [String], message: String) async -> CLIResult {
        let token = CancelToken()
        let panel = ProgressPanel(message: message) { token.cancel() }
        let shower = Task { @MainActor in
            try? await Task.sleep(for: .milliseconds(400))
            if !Task.isCancelled {
                panel.show()
            }
        }
        let result = await run(verb, cancel: token)
        shower.cancel()
        panel.close()
        return result
    }

    /// Ask for the name Lucille will see, starting from a suggestion. Nil when the user cancels.
    private func askForAlias(suggestion: String, folderName: String, taken: Set<String>) -> String? {
        var current = suggestion
        var note: String?
        while true {
            let alert = NSAlert()
            alert.messageText = "Name Lucille will see"
            alert.informativeText = HostText.aliasPrompt(folderName: folderName, note: note)
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

    /// Ask before adding a folder that is the home folder, holds it, or is the whole disk. True
    /// when the user chooses Add Anyway; Choose a Smaller Folder, the default, is false.
    private func confirmBroadFolder(_ kind: BroadFolder) -> Bool {
        let question = HostText.broadFolderQuestion(
            kind, diskName: FileManager.default.displayName(atPath: "/")
        )
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = question.title
        alert.informativeText = question.text
        alert.addButton(withTitle: "Choose a Smaller Folder")
        alert.addButton(withTitle: "Add Anyway")
        NSApp.activate()
        return alert.runModal() == .alertSecondButtonReturn
    }

    private func showNote(title: String, text: String) {
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = text
        alert.addButton(withTitle: "OK")
        NSApp.activate()
        alert.runModal()
    }

    private func showFailure(_ explanation: Explanation, action: String) {
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = "Lucille Host could not \(action)."
        alert.informativeText = explanation.message
            + (explanation.detail.map { "\n\nDetails: \($0)" } ?? "")
        alert.addButton(withTitle: "OK")
        NSApp.activate()
        alert.runModal()
    }
}
