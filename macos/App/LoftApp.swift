import AppKit
import SwiftUI

/// Loft, the menu bar app that runs the host. It has no window and no Dock icon (LSUIElement);
/// everything it does is in this menu.
@main
struct LoftApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @StateObject private var model = HostModel()

    var body: some Scene {
        MenuBarExtra {
            HostMenu(model: model)
        } label: {
            Image(nsImage: HostModel.icon(paused: model.paused))
                .accessibilityLabel(HostText.statusItemName(paused: model.paused))
        }
    }
}

/// Says what the app is for and where the menu is when the app opens, and when it is opened again
/// while it runs: it has no window and no Dock icon, so otherwise nothing would seem to happen.
/// Quit is offered too, for when the menu bar has no room for the icon.
@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    private var showing = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        Task { @MainActor in
            try? await Task.sleep(for: .seconds(1))
            self.showWhereTheMenuIs()
        }
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        showWhereTheMenuIs()
        return false
    }

    private func showWhereTheMenuIs() {
        guard !showing else {
            return
        }
        showing = true
        defer { showing = false }
        let alert = NSAlert()
        alert.messageText = HostText.welcomeTitle
        alert.informativeText = HostText.welcomeText
        alert.addButton(withTitle: "OK")
        alert.addButton(withTitle: "Quit Loft")
        NSApp.activate()
        if alert.runModal() == .alertSecondButtonReturn {
            NSApplication.shared.terminate(nil)
        }
    }
}

struct HostMenu: View {
    @ObservedObject var model: HostModel

    var body: some View {
        Text(model.title)
        Text(model.statusLine)
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
                Text("Once connected, Lucille sees which abilities are on and your folders' names")
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
        Divider()
        Button("Connect to Lucille (coming soon)") {}
            .disabled(true)
        Divider()
        Button("Quit Loft") { NSApplication.shared.terminate(nil) }
            .keyboardShortcut("q")
    }
}
