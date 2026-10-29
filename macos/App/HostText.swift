import Foundation

/// One catalog operation as `lucille-host ops list` prints it. The menu calls them abilities.
struct Ability: Equatable, Identifiable, Sendable {
    let name: String
    let state: String

    var id: String { name }

    /// What the menu calls it: a few plain words, or the catalog name if the app has none.
    var title: String { HostText.abilityTitles[name] ?? name }

    /// Whether the Abilities menu lists it. Diagnostic and always-on operations are left out, and
    /// so is a name the policy holds that this release does not have.
    var isShown: Bool { !HostText.unlistedAbilities.contains(name) && !state.hasPrefix("in the policy") }

    /// Shown with a checkmark: the local policy names it.
    var isOn: Bool { state.hasPrefix("enabled") || state.hasPrefix("in the policy") }

    /// Whether the menu may switch it: off when it is on, on when nothing stops it.
    var canChange: Bool { isOn || state == "disabled" || state.hasPrefix("disabled: an upgrade") }

    /// A plain sentence for a state that is more than on or off.
    var note: String? {
        if state == "enabled" || state == "disabled" {
            return nil
        }
        if state.hasPrefix("disabled: an upgrade") {
            return "An update changed this ability, so it was turned off."
        }
        if state.hasPrefix("enabled, but not on this computer") {
            return "On, but this Mac lacks a program it needs."
        }
        if state.hasPrefix("disabled; not on this computer") {
            return "This Mac lacks a program it needs."
        }
        if state.hasPrefix("in the policy") {
            return "Not in this version of Lucille Host."
        }
        return state
    }
}

/// One folder alias as `lucille-host roots list` prints it.
struct Folder: Equatable, Identifiable, Sendable {
    let alias: String
    let path: String

    var id: String { alias }
}

/// What the app tells the user about a run that failed: one short sentence, and the host's own
/// words, if it gave any, as a detail.
struct Explanation: Equatable, Sendable {
    let message: String
    let detail: String?
}

/// A folder the host adds only once the user confirms (`--allow-home-or-root`).
enum BroadFolder: Equatable, Sendable {
    case home
    case holdsHome
    case disk
}

/// Reading what the host's command line prints. It has no machine-readable output yet, so this
/// follows its text exactly (`src/lucille_host/cli.py`); the self-test checks it against the
/// real command line on every build.
enum HostText {
    private static let program = "lucille-host"

    /// The menu's words for each catalog operation it lists. Every operation in the catalog is
    /// either here or in `unlistedAbilities` (`tests/test_macos_build.py` checks that).
    static let abilityTitles: [String: String] = [
        "files.sha256": "Check a file's fingerprint",
    ]

    /// Operations the Abilities menu does not list: `host.describe`, which is on from the start
    /// and is described in the menu instead, and `host.sleep`, which is for testing.
    static let unlistedAbilities: Set<String> = ["host.describe", "host.sleep"]

    /// "lucille-host 0.1.0" gives "0.1.0".
    static func version(fromVersionOutput text: String) -> String? {
        let line = text.trimmingCharacters(in: .whitespacesAndNewlines)
        let prefix = program + " "
        guard line.hasPrefix(prefix), !line.contains("\n") else {
            return nil
        }
        let version = String(line.dropFirst(prefix.count))
        return version.isEmpty || version.contains(" ") ? nil : version
    }

    /// Each "name  state" line of `ops list`.
    static func abilities(fromList text: String) -> [Ability] {
        pairs(text).map { Ability(name: $0.0, state: $0.1) }
    }

    /// Each "alias  path" line of `roots list`.
    static func folders(fromList text: String) -> [Folder] {
        pairs(text).map { Folder(alias: $0.0, path: $0.1) }
    }

    /// Whether `roots add` refused because the folder is the home folder, holds it, or is "/".
    /// The user may then confirm, and the app adds `--allow-home-or-root`.
    static func needsHomeConfirmation(_ result: CLIResult) -> Bool {
        result.status == 1 && !result.timedOut && !result.cancelled
            && result.stderr.contains("--allow-home-or-root")
    }

    /// Whether `path` is one the host adds only with the user's confirmation, as far as the app
    /// can tell from the path: the home folder, a folder that holds it, or "/". The host decides;
    /// this lets the app ask before the user names the folder.
    static func broadFolder(path: String, home: String) -> BroadFolder? {
        let folder = standardized(path)
        let homeFolder = standardized(home)
        if folder == "/" {
            return .disk
        }
        if folder == homeFolder {
            return .home
        }
        if homeFolder.hasPrefix(folder + "/") {
            return .holdsHome
        }
        return nil
    }

    /// The question the app asks before it adds a broad folder: a title and a sentence for each
    /// kind. `diskName` is what Finder calls the startup disk.
    static func broadFolderQuestion(_ kind: BroadFolder, diskName: String) -> (title: String, text: String) {
        let inside = "your Documents, Desktop and Downloads folders, your Library, and other apps' files"
        switch kind {
        case .home:
            return (
                "Add your whole home folder?",
                "This is your home folder. It holds \(inside). Abilities you turn on could use any file in it. Most people choose one folder inside it instead."
            )
        case .holdsHome:
            return (
                "Add a folder that holds your home folder?",
                "This folder holds your home folder, with \(inside). Abilities you turn on could use any file in it. Most people choose one smaller folder instead."
            )
        case .disk:
            return (
                "Add your whole startup disk?",
                "\(diskName) is your whole startup disk. It holds your home folder, with \(inside), and every other file on this Mac. Abilities you turn on could use any file on it. Most people choose one folder instead."
            )
        }
    }

    /// The name dialog's text, with the sentence `note` first when the app had to change the name.
    static func aliasPrompt(folderName: String, note: String?) -> String {
        let purpose = "Lucille will know \u{201C}\(folderName)\u{201D} only by this name. It never learns where the folder is on your Mac."
        let rules = "Use lowercase letters, digits, - and _, up to \(Alias.maxLength) characters."
        return [note, purpose, rules].compactMap { $0 }.joined(separator: "\n\n")
    }

    /// What a run was for, when that changes what the user is told.
    enum Activity: Sendable {
        case addingFolder
        case other
    }

    /// What to tell the user when a run failed: a plain sentence for each refusal the host's
    /// command line gives, and its own words as the detail.
    static func explain(_ result: CLIResult, during activity: Activity = .other) -> Explanation {
        if result.status == CLIResult.notStarted {
            return Explanation(message: "Lucille Host is damaged. Download it again.", detail: result.stderr)
        }
        if result.timedOut {
            let message: String
            switch activity {
            case .addingFolder:
                message = "Checking this folder took too long. If it is on a network or external drive, check that it is connected."
            case .other:
                message = "Lucille Host's helper program did not answer in time, so it was stopped."
            }
            return Explanation(message: message, detail: nil)
        }
        if result.cancelled {
            return Explanation(message: "Stopped.", detail: nil)
        }
        let reason = reasons(result.stderr)
        guard !reason.isEmpty else {
            return Explanation(
                message: "Something went wrong in Lucille Host's helper program.",
                detail: "It stopped with exit status \(result.status)."
            )
        }
        return Explanation(message: plain(reason), detail: reason)
    }

    /// The host's reason for a refusal, from its stderr: the lines that are not a usage summary,
    /// without the program's name, in the host's own words and case.
    static func reasons(_ stderr: String) -> String {
        var found: [String] = []
        for raw in stderr.split(separator: "\n", omittingEmptySubsequences: true) {
            if raw.hasPrefix(" ") || raw.hasPrefix("usage:") {
                continue
            }
            var line = raw.trimmingCharacters(in: .whitespaces)
            if let marker = line.range(of: ": error: ") {
                line = String(line[marker.upperBound...])
            } else if line.hasPrefix(program + ": ") {
                line = String(line.dropFirst(program.count + 2))
            }
            if !line.isEmpty {
                found.append(line)
            }
        }
        return found.joined(separator: " ")
    }

    /// A plain sentence for each refusal `src/lucille_host/cli.py` can give.
    private static func plain(_ reason: String) -> String {
        let taken = " is already a folder alias; remove it first"
        if reason.hasSuffix(taken) {
            let alias = String(reason.dropLast(taken.count))
            return "You already have a folder named \(alias). Choose another name."
        }
        if reason.hasPrefix("the policy holds at most ") {
            let limit = reason.dropFirst("the policy holds at most ".count).prefix { $0.isNumber }
            return "Lucille Host can use at most \(limit) folders. Remove one before you add another."
        }
        if reason.hasSuffix(" is where lucille-host keeps its own files") {
            return "That folder holds Lucille Host's own settings, so it can't be added."
        }
        if reason.hasSuffix(" is not a folder") {
            return "That folder is no longer there."
        }
        if reason.hasPrefix("the policy file ") && reason.contains(" is invalid: ") {
            return "Lucille Host's settings file can't be read."
        }
        if reason.hasPrefix("cannot lock ") {
            return "Lucille Host couldn't lock its settings file, so nothing was changed. Try again."
        }
        if reason.hasPrefix("no folder alias ") {
            return "That folder was already removed."
        }
        if reason.hasSuffix(" is not enabled") {
            return "That ability was already off."
        }
        if reason.hasSuffix(" is not in this release's catalog") {
            return "This version of Lucille Host doesn't have that ability."
        }
        if reason.contains(" cannot run here: ") {
            return "This Mac lacks a program that ability needs."
        }
        if reason.hasPrefix("an alias is ") {
            return "That name can't be used."
        }
        if reason.contains("--allow-home-or-root") {
            return "That folder is your home folder, holds it, or is your whole disk."
        }
        return "Something went wrong in Lucille Host's helper program."
    }

    private static func standardized(_ path: String) -> String {
        let clean = (path as NSString).standardizingPath
        return clean.isEmpty ? path : clean
    }

    private static func pairs(_ text: String) -> [(String, String)] {
        var found: [(String, String)] = []
        for line in text.split(separator: "\n", omittingEmptySubsequences: true) {
            guard let gap = line.range(of: "  ") else {
                continue
            }
            let head = String(line[..<gap.lowerBound])
            let tail = String(line[gap.upperBound...])
            if !head.isEmpty && !tail.isEmpty && !head.contains(" ") {
                found.append((head, tail))
            }
        }
        return found
    }
}
