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

    /// Whether the host accepts `alias`.
    static func isValid(_ alias: String) -> Bool {
        let scalars = Array(alias.unicodeScalars)
        guard let first = scalars.first, scalars.count <= maxLength, isLetter(first) else {
            return false
        }
        return scalars.allSatisfy { isLetter($0) || isDigit($0) || $0 == dash || $0 == underscore }
    }

    /// A valid alias made from a folder's name: accents removed, lowercase, every other
    /// character replaced by "-", runs of "-" joined, everything before the first letter
    /// dropped, at most 32 characters, and no "-" or "_" at the end. "folder" when nothing is
    /// left.
    static func suggest(forFolderName name: String) -> String {
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
        while let last = kept.last, last == dash || last == underscore {
            kept.removeLast()
        }
        var view = String.UnicodeScalarView()
        view.append(contentsOf: kept)
        let suggestion = String(view)
        return isValid(suggestion) ? suggestion : fallback
    }

    private static func isLetter(_ scalar: Unicode.Scalar) -> Bool {
        scalar.value >= 0x61 && scalar.value <= 0x7A
    }

    private static func isDigit(_ scalar: Unicode.Scalar) -> Bool {
        scalar.value >= 0x30 && scalar.value <= 0x39
    }
}
