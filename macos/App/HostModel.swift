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
    @Published private(set) var problem: String?

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

    var statusLine: String {
        paused ? "Paused: Lucille can start nothing" : "Not paused"
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
            failing: "turn \(on ? "on" : "off") \(ability.name)"
        )
    }

    func removeFolder(_ folder: Folder) {
        perform(["roots", "remove", folder.alias], failing: "remove the folder \(folder.alias)")
    }

    /// Choose a folder in the standard Open panel, name it, and add it.
    func addFolder() {
        NSApp.activate()
        let panel = NSOpenPanel()
        panel.title = "Add a Folder"
        panel.message = "Choose a folder Lucille may work in. Abilities you turn on can use the files in it."
        panel.prompt = "Choose"
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        panel.canCreateDirectories = false
        guard panel.runModal() == .OK, let url = panel.url else {
            return
        }
        let path = url.path
        let suggestion = Alias.suggest(forFolderName: url.lastPathComponent)
        guard let alias = askForAlias(suggestion: suggestion, path: path) else {
            return
        }
        Task { await addRoot(alias: alias, path: path) }
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
        abilities = opsRun.succeeded ? HostText.abilities(fromList: opsRun.stdout) : []
        folders = rootsRun.succeeded ? HostText.folders(fromList: rootsRun.stdout) : []
        problem = results.first(where: { !$0.succeeded }).map(HostText.failureMessage)
        paused = cli.isPaused
    }

    private func run(_ verb: [String]) async -> CLIResult {
        let cli = self.cli
        return await Task.detached(priority: .userInitiated) { cli.run(verb) }.value
    }

    private func perform(_ verb: [String], failing action: String) {
        Task {
            let result = await run(verb)
            if !result.succeeded {
                showFailure(result, action: action)
            }
            await reload()
        }
    }

    private func addRoot(alias: String, path: String) async {
        var result = await run(["roots", "add", alias, path])
        if HostText.needsHomeConfirmation(result) {
            guard confirmBroadFolder(path: path) else {
                await reload()
                return
            }
            result = await run(["roots", "add", alias, path, "--allow-home-or-root"])
        }
        if !result.succeeded {
            showFailure(result, action: "add the folder")
        }
        await reload()
    }

    /// Ask for the folder's alias, starting from a suggestion. Nil when the user cancels.
    private func askForAlias(suggestion: String, path: String) -> String? {
        var current = suggestion
        while true {
            let alert = NSAlert()
            alert.messageText = "Name this folder for Lucille"
            alert.informativeText = """
                Lucille will call \(path) by this name. Use lowercase letters, digits, - and _, \
                starting with a letter, at most \(Alias.maxLength) characters.
                """
            let field = NSTextField(frame: NSRect(x: 0, y: 0, width: 280, height: 24))
            field.stringValue = current
            alert.accessoryView = field
            alert.addButton(withTitle: "Add Folder")
            alert.addButton(withTitle: "Cancel")
            alert.window.initialFirstResponder = field
            NSApp.activate()
            guard alert.runModal() == .alertFirstButtonReturn else {
                return nil
            }
            let typed = field.stringValue.trimmingCharacters(in: .whitespaces)
            if Alias.isValid(typed) {
                return typed
            }
            current = typed.isEmpty ? suggestion : typed
            let refusal = NSAlert()
            refusal.alertStyle = .warning
            refusal.messageText = "That name cannot be used"
            refusal.informativeText = """
                A folder's name for Lucille uses lowercase letters, digits, - and _, starts with a \
                letter, and has at most \(Alias.maxLength) characters.
                """
            refusal.runModal()
        }
    }

    /// Ask before adding a folder that is the home folder, holds it, or is the top of the disk.
    private func confirmBroadFolder(path: String) -> Bool {
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = "Add this whole folder?"
        alert.informativeText = """
            \(path) is your home folder, holds it, or is the top of the disk. Abilities you turn \
            on could then use any file in it. Choose a smaller folder unless you mean this one.
            """
        alert.addButton(withTitle: "Cancel")
        alert.addButton(withTitle: "Add Anyway")
        NSApp.activate()
        return alert.runModal() == .alertSecondButtonReturn
    }

    private func showFailure(_ result: CLIResult, action: String) {
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = "Lucille Host could not \(action)."
        alert.informativeText = HostText.failureMessage(result)
        alert.addButton(withTitle: "OK")
        NSApp.activate()
        alert.runModal()
    }
}
