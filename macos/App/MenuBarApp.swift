import AppKit
import SwiftUI

/// The menu bar app that runs the host. It has no window and no Dock icon (LSUIElement);
/// everything it does is in this menu, the pairing window, the Activity window and the notes it
/// shows.
@main
struct MenuBarApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @StateObject private var model = HostModel.shared
    @StateObject private var connection = Connection.shared

    var body: some Scene {
        MenuBarExtra {
            HostMenu(model: model, connection: connection)
        } label: {
            Image(nsImage: HostModel.icon(paused: model.paused, attention: connection.needsAttention))
                .accessibilityLabel(HostText.statusItemName(paused: model.paused))
        }
    }
}

/// Says what the app is for and where the menu is when the app opens, and when it is opened again
/// while it runs: it has no window and no Dock icon, so otherwise nothing would seem to happen.
/// Quit is offered too, for when the menu bar has no room for the icon. Once that note is first
/// closed, `Connection` says whether the host is paused and picks up a pairing left unfinished, so
/// nothing it shows at launch races the note.
@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    private var noteQueued = false
    private var launchHandled = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        Connection.shared.launch()
        Task { @MainActor in
            try? await Task.sleep(for: .seconds(1))
            self.showWhereTheMenuIs()
        }
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
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

struct HostMenu: View {
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
        Divider()
        Button(HostText.quitApp) { connection.quitChosen() }
            .keyboardShortcut("q")
    }
}
