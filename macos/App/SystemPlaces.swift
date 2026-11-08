import AppKit

/// The app's one opener (decisions/0002 §20): it shows the user a place in macOS, and only from a
/// click, a note's button or a button of Set Up's Folders page. The places are a closed list,
/// `SystemPlace`, and the address of each is written here, once: two pages of System Settings,
/// and the Applications folder. Nothing read at run time makes an address: what the app sees
/// only picks one of these places, and no text or request from Lucille offers one.
/// tools/guards/macos_lint.py holds this file to one call that opens, with no configuration and
/// no other application, of exactly these addresses, and refuses that call in every other file.
@MainActor
enum SystemPlaces {
    /// Show `place`. False when macOS could not open it.
    static func open(_ place: SystemPlace) -> Bool {
        let url: URL?
        switch place {
        case .dateAndTime:
            url = URL(string: "x-apple.systempreferences:com.apple.Date-Time-Settings.extension")
        case .filesAndFolders:
            url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_FilesAndFolders")
        case .applicationsFolder:
            url = URL(fileURLWithPath: "/Applications", isDirectory: true)
        }
        guard let url else {
            return false
        }
        return NSWorkspace.shared.open(url)
    }
}
