import Foundation

// The rules of the app's one opener (decisions/0002 §20): which problem offers which place in
// macOS, and the words of each place's button. Foundation only, so the self-test runs them without
// a screen. `SystemPlaces` opens a place, from that button's click; nothing here opens anything. A
// place comes only from what the app itself sees: the problem `run` reports, macOS's refusal of a
// folder, where this copy is. No text or request from Lucille, and no refusal of a request, offers
// one; the clock's place follows the clock state `run` reports, which the hub's refusals set.

/// A place in macOS the app can show the user, from a click. The list is closed and its cases
/// carry no value, so nothing read at run time can make an address: what the app sees only picks
/// one of these, and `SystemPlaces` holds the address of each.
enum SystemPlace: Equatable, Sendable {
    /// System Settings, General, Date & Time.
    case dateAndTime
    /// System Settings, Privacy & Security, Files & Folders. Never Full Disk Access.
    case filesAndFolders
    /// The Applications folder, in the Finder.
    case applicationsFolder
}

enum PlaceRules {
    /// The place the note behind run's Needs attention line offers: Date & Time for a clock that
    /// is off, and nothing else. Every case is named, so a new one must be decided here.
    static func place(for problem: Problem) -> SystemPlace? {
        switch problem {
        case .clock:
            return .dateAndTime
        case .halted, .notResponding, .policyInvalid, .policyStuck, .hubUnreachable, .readStuck, .leftConnection:
            return nil
        }
    }

    /// The place a folder of Set Up's Folders page offers, from what `roots check` said of it:
    /// Files & Folders when macOS's privacy protection refuses the host the folder, and nothing
    /// else. A folder whose own permissions refuse it is not System Settings' to change.
    static func place(for state: FolderCheck.State) -> SystemPlace? {
        switch state {
        case .deniedByMacOS:
            return .filesAndFolders
        case .ok, .denied, .missing, .error:
            return nil
        }
    }

    /// The place the note of a copy that may not run offers: the Applications folder, for every
    /// copy the app judges not to be in it (`notInApplications`), where the note says to drag it.
    /// A renamed copy is in that folder already.
    static func place(for placement: Placement) -> SystemPlace? {
        switch placement {
        case .notInApplications:
            return .applicationsFolder
        case .placed, .renamed:
            return nil
        }
    }

    /// The place the note of a run that failed offers: Files & Folders when, while a folder was
    /// added, the host found no folder that the app can see or is refused too, which is macOS's
    /// privacy protection at work (`HostText.refusal`). Decided on the refusal's code alone, never
    /// on its words; and only for the app's own verbs, whose refusals this is.
    static func place(for error: HostError, during activity: HostText.Activity) -> SystemPlace? {
        if case .addingFolder(let access, _) = activity, error.code == "not_a_folder", access != .missing {
            return .filesAndFolders
        }
        return nil
    }
}

/// The opener's words. The app's name is read from its Info.plist (`HostText.appName`).
enum PlaceText {
    private static let appName = HostText.appName

    /// The button that shows `place`.
    static func button(_ place: SystemPlace) -> String {
        switch place {
        case .dateAndTime:
            return "Open Date & Time Settings"
        case .filesAndFolders:
            return "Open Privacy Settings"
        case .applicationsFolder:
            return "Show Applications Folder"
        }
    }

    /// What the note says first when it comes again because macOS could not open `place`. The
    /// rest of the note already says how to get there by hand.
    static func couldNotOpen(_ place: SystemPlace) -> String {
        switch place {
        case .dateAndTime, .filesAndFolders:
            return "\(appName) couldn't open System Settings."
        case .applicationsFolder:
            return "\(appName) couldn't open your Applications folder."
        }
    }
}
