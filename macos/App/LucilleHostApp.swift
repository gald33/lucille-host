import AppKit
import SwiftUI

/// Lucille Host's menu bar app. It has no window and no Dock icon (LSUIElement); everything it
/// does is in this menu.
@main
struct LucilleHostApp: App {
    @StateObject private var model = HostModel()

    var body: some Scene {
        MenuBarExtra {
            HostMenu(model: model)
        } label: {
            Image(systemName: model.paused ? "pause.circle" : "desktopcomputer")
                .accessibilityLabel(model.paused ? "Lucille Host, paused" : "Lucille Host")
        }
    }
}

struct HostMenu: View {
    @ObservedObject var model: HostModel

    var body: some View {
        Text(model.title)
        Text(model.statusLine)
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
        Button("Quit Lucille Host") { NSApplication.shared.terminate(nil) }
            .keyboardShortcut("q")
    }
}
