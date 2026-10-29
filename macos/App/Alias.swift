import Foundation

/// Folder aliases: the short name Lucille uses for a folder the user adds.
///
/// The host accepts an alias that matches `[a-z][a-z0-9_-]{0,31}` (`valid_alias` in
/// `src/lucille_host/policy.py`). The app suggests one made from the folder's name, and the user
/// can change it before the folder is added.
enum Alias {
    static let maxLength = 32
    static let fallback = "folder"

    private static let dash: Unicode.Scalar = "-"
    private static let underscore: Unicode.Scalar = "_"

    /// What the app does with a name the user typed in the name dialog.
    enum Review: Equatable, Sendable {
        /// Use this name.
        case use(String)
        /// Show the dialog again with this name in it, and this sentence above it.
        case offer(String, because: String)
    }

    /// Whether the host accepts `alias`.
    static func isValid(_ alias: String) -> Bool {
        let scalars = Array(alias.unicodeScalars)
        guard let first = scalars.first, scalars.count <= maxLength, isLetter(first) else {
            return false
        }
        return scalars.allSatisfy { isLetter($0) || isDigit($0) || $0 == dash || $0 == underscore }
    }

    /// A valid alias made from a folder's name, and not one of `taken`: accents removed,
    /// lowercase, every other character replaced by "-", runs of "-" joined, everything before
    /// the first letter dropped, at most 32 characters, and no "-" or "_" at the end. "folder"
    /// when nothing is left. If the result is taken, "-2", "-3" and so on is added.
    static func suggest(forFolderName name: String, taken: Set<String> = []) -> String {
        unique(base(forFolderName: name), taken: taken)
    }

    /// What to do with `typed`, the name the user gave a folder. A valid name nobody uses is
    /// taken as it is. Anything else is turned into one the host accepts, which the user sees
    /// before anything is added.
    static func review(typed: String, suggestion: String, taken: Set<String>) -> Review {
        let name = typed.trimmingCharacters(in: .whitespacesAndNewlines)
        if name.isEmpty {
            return .offer(unique(suggestion, taken: taken), because: "Type a name for this folder.")
        }
        if isValid(name) {
            if !taken.contains(name) {
                return .use(name)
            }
            return .offer(
                unique(name, taken: taken),
                because: "You already have a folder named \(name). Choose another name."
            )
        }
        let fixed = suggest(forFolderName: name, taken: taken)
        return .offer(
            fixed,
            because: "Lucille Host changed \u{201C}\(name)\u{201D} to \u{201C}\(fixed)\u{201D}, a name it can use. Add it, or change it."
        )
    }

    private static func base(forFolderName name: String) -> String {
        let folded = name.folding(options: [.diacriticInsensitive, .widthInsensitive], locale: nil)
            .lowercased()
        var kept: [Unicode.Scalar] = []
        for scalar in folded.unicodeScalars {
            let usable = isLetter(scalar) || isDigit(scalar) || scalar == dash || scalar == underscore
            let next = usable ? scalar : dash
            if kept.isEmpty && !isLetter(next) {
                continue
            }
            if next == dash && kept.last == dash {
                continue
            }
            kept.append(next)
        }
        if kept.count > maxLength {
            kept = Array(kept.prefix(maxLength))
        }
        trimEnd(&kept)
        let suggestion = string(kept)
        return isValid(suggestion) ? suggestion : fallback
    }

    /// `alias` itself if nobody uses it, else the first of `alias-2`, `alias-3`, ... that is free,
    /// shortened so that it stays within 32 characters.
    private static func unique(_ alias: String, taken: Set<String>) -> String {
        if !taken.contains(alias) {
            return alias
        }
        var number = 2
        while true {
            let suffix = Array("-\(number)".unicodeScalars)
            var stem = Array(alias.unicodeScalars.prefix(maxLength - suffix.count))
            trimEnd(&stem)
            let candidate = string(stem + suffix)
            if !taken.contains(candidate) && isValid(candidate) {
                return candidate
            }
            number += 1
        }
    }

    private static func trimEnd(_ scalars: inout [Unicode.Scalar]) {
        while let last = scalars.last, last == dash || last == underscore {
            scalars.removeLast()
        }
    }

    private static func string(_ scalars: [Unicode.Scalar]) -> String {
        var view = String.UnicodeScalarView()
        view.append(contentsOf: scalars)
        return String(view)
    }

    private static func isLetter(_ scalar: Unicode.Scalar) -> Bool {
        scalar.value >= 0x61 && scalar.value <= 0x7A
    }

    private static func isDigit(_ scalar: Unicode.Scalar) -> Bool {
        scalar.value >= 0x30 && scalar.value <= 0x39
    }
}
