import Foundation

// Remove <name>… (decisions/0002 section 17): its words, and what the app makes of the host's
// answers between its steps. Foundation only, so the self-test runs them without a screen.
// `Connection` runs the steps; nothing here does.

/// How a Remove ended, for the note it ends with.
enum RemovalEnd: Equatable, Sendable {
    /// `uninstall-data` deleted the host's data. `lucilleTold` is what `forget` said, nil when
    /// there was no pair to tell Lucille about; `trashed`, whether the app went to the Trash;
    /// `login`, what came of taking the login item away.
    case removed(lucilleTold: Bool?, trashed: Bool, login: LoginRemoval)
    /// `forget` did not end the pair: nothing was deleted, and the app stays.
    case notDisconnected(VerbOutcome)
    /// `forget` ended the pair, and `uninstall-data` did not delete the data: the app stays.
    case notDeleted(VerbOutcome)
}

enum RemoveRules {
    /// How many lines of the host's log Save a Copy of the Activity Log… asks for: more than the
    /// host keeps (six files of at most 8 MB, and no record is as short as 48 bytes), so the copy
    /// holds every line there is.
    static let logLimit = 1_000_000
    static let logVerb = ["--json", "log", "--limit", String(logLimit)]
    static let pauseVerb = ["--json", "pause"]
    static let forgetVerb = ["--json", "forget"]
    static let killVerb = ["--json", "kill"]
    static let uninstallVerb = ["--json", "uninstall-data", "--yes"]
    /// The verbs the app may start once the user has chosen Remove, until it ends: the pause,
    /// Stop Now's `kill`, `forget`, and `uninstall-data`. Any other verb started then (a `status`
    /// read, the menu's own reads) could write into the profile's folders while they are deleted,
    /// so `HostCLI` refuses it (`HostCLI.beginRemoval`).
    static let removalVerbs: [[String]] = [pauseVerb, killVerb, forgetVerb, uninstallVerb]

    /// Whether Remove goes on after `forget`: the pair is gone, as Disconnect judges it. `forget`
    /// forgot it, or left the work rooms of one already gone, or found none (`none`, which a
    /// disconnecting shell takes as done, spec/CLI.md §7). Anything else keeps the pair, and so
    /// deletes nothing: `uninstall-data` would refuse it.
    static func forgetEnded(_ outcome: VerbOutcome) -> Bool {
        switch outcome {
        case .done:
            return outcome.forget != nil
        case .refused(let error):
            return error.code == "none"
        default:
            return false
        }
    }

    /// Whether Lucille was told: what `forget` said of a pair it forgot; nil when there was no
    /// pair to tell it about (`left`, `none`).
    static func lucilleTold(_ outcome: VerbOutcome) -> Bool? {
        if case .forgotten(_, let told)? = outcome.forget {
            return told
        }
        return nil
    }

    /// Whether `uninstall-data` deleted the data of the profile `name`, the app's own: it exited
    /// 0 with an `uninstalled` object that names that profile.
    static func deleted(_ outcome: VerbOutcome, named name: String) -> Bool {
        outcome.uninstalled == name
    }

    /// The note a Remove ends with.
    static func note(_ end: RemovalEnd) -> ConnectionNote {
        switch end {
        case .removed(let told, let trashed, let login):
            var said: [String] = []
            switch told {
            case true?:
                said.append(RemoveText.toldText)
            case false?:
                said.append(RemoveText.notToldText)
            case nil:
                break
            }
            said.append(RemoveText.deletedText)
            said.append(trashed ? RemoveText.trashedText : RemoveText.notTrashedText)
            if let loginSaid = LoginText.afterDisconnect(login) {
                said.append(loginSaid)
            }
            return ConnectionNote(title: RemoveText.removedTitle, text: said.joined(separator: " "), detail: nil)
        case .notDisconnected(let outcome):
            let disconnect = ConnectionText.disconnected(outcome)
            return ConnectionNote(
                title: RemoveText.notRemovedTitle,
                text: disconnect.text + " " + RemoveText.nothingDeletedText,
                detail: disconnect.detail
            )
        case .notDeleted(let outcome):
            var text = RemoveText.couldNotDeleteText
            var detail: String?
            switch outcome {
            case .refused(let error):
                let sentence = RemoveText.refusal(code: error.code, message: error.message)
                text += " " + sentence
                detail = error.message == sentence ? nil : error.message
            case .done:
                // Exit 0 with no `uninstalled` that names the app's own profile is not the answer it asked for.
                text += " " + ConnectionText.appBug
            default:
                text += " " + RemoveText.uninstallUnknown
            }
            return ConnectionNote(title: RemoveText.notRemovedTitle, text: text + " " + RemoveText.tryAgainText, detail: detail)
        }
    }
}

/// Every sentence Remove says. The app's name is read from the bundle (`HostText.appName`).
enum RemoveText {
    private static let appName = HostText.appName

    static let menuItem = "Remove \(appName)…"
    static let questionTitle = "Remove \(appName) from this Mac?"
    /// decisions/0002 section 17, step 1, as the record quotes it, and what else `uninstall-data`
    /// deletes with the settings: the saved app passwords and the activity log. What Lucille keeps
    /// is Lucille's policy as of 2026-10-29, which this repository does not prove (PRIVACY.md).
    static let questionText = "This disconnects this Mac from Lucille, deletes \(appName)'s saved connection and settings from this Mac, its saved app passwords and its activity log included, and moves the app to the Trash. Lucille keeps results it already received for up to 24 hours, and its database backups are not blanked."
    static let removeButton = "Remove \(appName)"
    static let saveLogButton = "Save a Copy of the Activity Log…"
    static let saveLogName = "\(appName) Activity Log.txt"
    static let saveLogFailedTitle = "\(appName) couldn't save a copy of the activity log"
    static let saveLogFailedText = "Nothing was removed."
    static let removing = "Removing \(appName)…"
    static let removingDetail = "A task that is running finishes first."
    static let stopNowButton = "Stop Now"
    static let removedTitle = "\(appName) was removed"
    static let toldText = "This Mac was disconnected, and Lucille was told."
    static let notToldText = "This Mac was disconnected, but Lucille wasn't reachable, so in Lucille, open Computers and remove this Mac."
    static let deletedText = "\(appName)'s saved connection and settings were removed from this Mac."
    static let trashedText = "The app is in the Trash."
    static let notTrashedText = "\(appName) couldn't move itself to the Trash: drag it there from your Applications folder."
    static let notRemovedTitle = "\(appName) wasn't removed"
    static let nothingDeletedText = "\(appName)'s saved connection and settings are still on this Mac, and it is paused: choose Resume Lucille to resume."
    static let couldNotDeleteText = "This Mac was disconnected from Lucille, but \(appName) couldn't delete all of its saved connection and settings, so the app stays."
    static let uninstallUnknown = "\(appName) couldn't tell whether they were removed."
    static let tryAgainText = "Choose Remove \(appName)… in Settings to try again."

    /// Remove's own words for a refusal of `uninstall-data`, by code (spec/CLI.md §10); any other
    /// code reads as `ConnectionText.refusal` does.
    static let refusals: [String: String] = [
        "busy": "Another task of \(appName)'s was still running.",
        "lock_failed": "\(appName) couldn't lock its settings.",
        "policy_lock_failed": "\(appName) couldn't lock its settings.",
        "held": "This Mac still has a connection to Lucille.",
        "other_device": "A disk or a folder is mounted inside \(appName)'s folders, and \(appName) deletes nothing through it. Eject it first.",
    ]

    static func refusal(code: String, message: String?) -> String {
        refusals[code] ?? ConnectionText.refusal(code: code, message: message, afterSAS: false)
    }
}
