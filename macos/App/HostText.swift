import Foundation

/// One catalog operation as `lucille-host ops list` prints it. The menu calls them abilities.
struct Ability: Equatable, Identifiable, Sendable {
    let name: String
    let state: String

    var id: String { name }

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

/// Reading what the host's command line prints. It has no machine-readable output yet, so this
/// follows its text exactly (`src/lucille_host/cli.py`); the self-test checks it against the
/// real command line on every build.
enum HostText {
    private static let program = "lucille-host"

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
        result.status == 1 && !result.timedOut && result.stderr.contains("--allow-home-or-root")
    }

    /// What to tell the user when a verb failed: the host's own reason, without the program's
    /// name or a usage summary, as one sentence.
    static func failureMessage(_ result: CLIResult) -> String {
        if result.timedOut {
            return "Lucille Host's helper program did not answer in time, so it was stopped."
        }
        if result.status == CLIResult.notStarted {
            return result.stderr
        }
        var reasons: [String] = []
        for raw in result.stderr.split(separator: "\n", omittingEmptySubsequences: true) {
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
                reasons.append(line)
            }
        }
        guard !reasons.isEmpty else {
            return "It stopped with exit status \(result.status) and gave no reason."
        }
        let text = reasons.joined(separator: " ")
        let sentence = text.prefix(1).uppercased() + text.dropFirst()
        return sentence.hasSuffix(".") ? sentence : sentence + "."
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
