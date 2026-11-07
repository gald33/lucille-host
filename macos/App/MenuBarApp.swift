import AppKit
import SwiftUI

/// The menu bar app that runs the host. It has no window and no Dock icon (LSUIElement);
/// everything it does is in this menu, the pairing window, the Set Up window, the Activity window
/// and the notes it shows. Only the copy at /Applications/<name>.app does any of it (decisions/0002 §13).
@main
struct MenuBarApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @StateObject private var model = HostModel.shared
    @StateObject private var connection = Connection.shared

    /// The one place the latch that lets the app start anything is opened, before anything could
    /// start: only for the copy at /Applications/<name>.app, the one `Placement` gives a token.
    init() {
        if case .placed(let token) = Placement.current {
            HostCLI.allowSpawns(token)
        }
    }

    var body: some Scene {
        MenuBarExtra {
            HostMenu(model: model, connection: connection)
        } label: {
            Image(nsImage: HostModel.icon(paused: model.paused, attention: connection.needsAttention))
                .accessibilityLabel(HostText.statusItemName(paused: model.paused))
        }
    }
}

/// Says what the app is for and where the menu is when the app opens with no confirmed pair, and
/// whenever it is opened again while it runs, paired or not: it has no window and no Dock icon, so
/// otherwise nothing would seem to happen (decisions/0002, What is built today). Quit is offered
/// too, for when the menu bar has no room for the icon. At launch the note waits for the first `status` read; a paired launch
/// shows none, and goes straight on. Once the note is first closed, or at once when paired,
/// `Connection` says whether the host is paused and picks up a pairing left unfinished, so nothing
/// it shows at launch races the note. A copy anywhere but /Applications/<name>.app shows its
/// placement note instead, each time, and never launches `Connection`.
@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    private var noteQueued = false
    private var launchHandled = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        guard Placement.current.isPlaced else {
            Task { @MainActor in
                try? await Task.sleep(for: .seconds(1))
                PlacementNote.show()
            }
            return
        }
        Connection.shared.onFirstStatusRead = { [weak self] paired in
            self?.launchRead(paired: paired)
        }
        Connection.shared.launch()
    }

    /// The first `status` read at launch has come. Paired, the app says nothing about its menu
    /// and goes on; not paired, it says where the menu is, and goes on once that note is closed.
    /// A reopen before it, whose note was closed, has handled the launch already.
    private func launchRead(paired: Bool) {
        guard !launchHandled else {
            return
        }
        if paired {
            launchHandled = true
            Connection.shared.afterWelcome()
        } else {
            showWhereTheMenuIs()
        }
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        guard Placement.current.isPlaced else {
            PlacementNote.show()
            return false
        }
        showWhereTheMenuIs()
        return false
    }

    /// While a child of the app lives, the app waits for it before it quits; its own Quit asks
    /// first while `run` runs, and a quit by Apple event never asks (`Connection.shouldTerminate`).
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        Connection.shared.shouldTerminate()
    }

    private func showWhereTheMenuIs() {
        guard !noteQueued else {
            return
        }
        noteQueued = true
        Alerts.shared.show(key: "welcome", front: true, {
            let alert = NSAlert()
            alert.messageText = HostText.welcomeTitle
            alert.informativeText = HostText.welcomeText
            alert.addButton(withTitle: "OK")
            alert.addButton(withTitle: HostText.quitApp)
            return alert
        }, done: { [weak self] response in
            guard let self else {
                return
            }
            self.noteQueued = false
            if response == .alertSecondButtonReturn {
                // The quit runs in a turn of its own, once this note's has ended.
                Connection.shared.quitChosen()
            }
            if !self.launchHandled {
                self.launchHandled = true
                Connection.shared.afterWelcome()
            }
        })
    }
}

/// What a copy that is not at /Applications/<name>.app says when it opens, when it is opened
/// again, and from its menu's one line (decisions/0002 §3.3 and §13): why it can't run, and how
/// to fix that. Its one button is Quit, through `Connection.quitChosen`, the app's one way to quit.
@MainActor
enum PlacementNote {
    static func show() {
        guard let note = ConnectionText.placement(Placement.current) else {
            return
        }
        Alerts.shared.show(key: "placement", front: true, {
            let alert = NSAlert()
            alert.alertStyle = .warning
            alert.messageText = note.title
            alert.informativeText = note.text
            alert.addButton(withTitle: ConnectionText.quitButton)
            return alert
        }, done: { _ in
            Connection.shared.quitChosen()
        })
    }
}

/// The menu. A copy that is not at /Applications/<name>.app shows only the line that says why it
/// can't run, which shows its note again, and Quit.
struct HostMenu: View {
    @ObservedObject var model: HostModel
    @ObservedObject var connection: Connection

    var body: some View {
        if Placement.current.isPlaced {
            HostItems(model: model, connection: connection)
        } else if let note = ConnectionText.placement(Placement.current) {
            Button(note.title) { PlacementNote.show() }
        }
        Divider()
        Button(HostText.quitApp) { connection.quitChosen() }
            .keyboardShortcut("q")
    }
}

/// Everything the menu of the copy at /Applications/<name>.app holds above Quit.
struct HostItems: View {
    @ObservedObject var model: HostModel
    @ObservedObject var connection: Connection

    var body: some View {
        Text(model.title)
        Text(connection.statusLine)
        if let reason = connection.reason {
            Button(reason) { connection.showAttention() }
        }
        if model.paused {
            Text(HostText.pausedNote)
        }
        if model.stuck {
            Text("A folder check is stuck. Reconnect the drive or restart your Mac.")
        }
        if let problem = model.problem {
            Button(problem.message) { model.showProblem() }
        }
        if let notice = connection.updateNotice {
            Button(notice) { connection.showUpdateNotice() }
            Button(ConnectionText.prepareToUpdateItem) { connection.prepareToUpdateChosen() }
        }
        Divider()
        if connection.operationRunning {
            Button(ConnectionText.stopWhatsRunningItem) { connection.stopWhatsRunningChosen() }
        }
        if model.paused {
            Button("Resume Lucille") { model.setPaused(false) }
        } else {
            Button("Pause Lucille") { model.setPaused(true) }
        }
        Menu("Abilities") {
            if !model.abilitiesRead {
                Text("Couldn't read abilities")
            } else if model.listedAbilities.isEmpty {
                Text("None to show")
            }
            ForEach(model.listedAbilities) { ability in
                Toggle(
                    ability.title,
                    isOn: Binding(
                        get: { ability.isOn },
                        set: { model.setAbility(ability, on: $0) }
                    )
                )
                .disabled(!ability.canChange)
                if let detail = ability.detail {
                    Text(detail)
                }
                if let note = ability.note {
                    Text(note)
                }
            }
            if model.describeIsOn {
                Divider()
                if connection.connected {
                    Text(ConnectionText.describeConnected)
                } else {
                    Text("Once connected, Lucille sees which abilities are on and your folders' names")
                }
            }
        }
        Divider()
        if connection.setUpOffered {
            Button(SetupText.menuItem) { connection.setUpChosen() }
        }
        Button("Add a Folder…") { model.addFolder() }
        Menu("Folders") {
            if !model.foldersRead {
                Text("Couldn't read your folders")
            } else if model.folders.isEmpty {
                Text("No folders yet")
            }
            ForEach(model.folders) { folder in
                Menu(folder.alias) {
                    Text(folder.path)
                    Button("Remove") { model.removeFolder(folder) }
                }
            }
        }
        Button(ConnectionText.activityItem) { connection.activityChosen() }
        // While connected: Open at Login, whose switch shows what macOS says (decisions/0002 §9),
        // and, while the settings file can't be read, Reset Settings… (§14).
        if connection.connected {
            Menu(LoginText.settingsMenu) {
                Toggle(
                    LoginText.openAtLoginItem,
                    isOn: Binding(
                        get: { connection.openAtLogin },
                        set: { connection.openAtLoginChosen($0) }
                    )
                )
                if model.problem?.offersReset == true {
                    Button(ConnectionText.resetSettingsItem) { model.resetSettingsChosen() }
                }
            }
        }
        Divider()
        if connection.items.connect {
            Button(ConnectionText.connectItem) { connection.connectChosen() }
                .disabled(!connection.items.enabled)
        }
        if connection.items.finish {
            Button(ConnectionText.finishItem) { connection.finishChosen() }
                .disabled(!connection.items.enabled)
        }
        if connection.items.stopConnecting {
            Button(ConnectionText.stopConnectingItem) { connection.stopConnectingChosen() }
                .disabled(!connection.items.enabled)
        }
        if connection.items.disconnect {
            Button(ConnectionText.disconnectItem) { connection.disconnectChosen() }
                .disabled(!connection.items.enabled)
        }
    }
}
