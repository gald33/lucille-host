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

    /// A valid alias made from a folder's name, and not one of `taken`: written in Latin letters
    /// where macOS can ("日本語" gives "ri-ben-yu"), accents removed, lowercase, every other
    /// character replaced by "-", runs of "-" joined, "-" and "_" trimmed from both ends,
    /// "folder-" put first when it starts with a digit ("2026 Taxes" gives "folder-2026-taxes"),
    /// and at most 32 characters. "folder" when nothing is left. If the result is taken, "-2",
    /// "-3" and so on is added.
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
        let folded = latin(name)
            .folding(options: [.diacriticInsensitive, .widthInsensitive], locale: nil)
            .lowercased()
        var kept: [Unicode.Scalar] = []
        for scalar in folded.unicodeScalars {
            let usable = isLetter(scalar) || isDigit(scalar) || scalar == dash || scalar == underscore
            let next = usable ? scalar : dash
            if next == dash && kept.last == dash {
                continue
            }
            kept.append(next)
        }
        trimStart(&kept)
        trimEnd(&kept)
        guard let first = kept.first else {
            return fallback
        }
        if !isLetter(first) {
            kept = Array("\(fallback)-".unicodeScalars) + kept
        }
        if kept.count > maxLength {
            kept = Array(kept.prefix(maxLength))
        }
        trimEnd(&kept)
        let suggestion = string(kept)
        return isValid(suggestion) ? suggestion : fallback
    }

    /// `name` in Latin letters, without accents, as ICU's "Any-Latin; Latin-ASCII" writes it. On
    /// a system whose Foundation lacks the transform, `name` as it is: then a name in another
    /// script gives "folder".
    private static func latin(_ name: String) -> String {
        #if canImport(Darwin)
        return name.applyingTransform(StringTransform("Any-Latin; Latin-ASCII"), reverse: false) ?? name
        #else
        return name
        #endif
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

    private static func trimStart(_ scalars: inout [Unicode.Scalar]) {
        while let first = scalars.first, first == dash || first == underscore {
            scalars.removeFirst()
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
