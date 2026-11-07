import Foundation

/// What a copy of the app at /Applications/<name>.app has, and no other copy: `Placement.judge`
/// is the one thing that makes one, and only for that copy. `HostCLI.allowSpawns` takes it, so
/// only that copy can open the latch that lets the app start anything (decisions/0002 §13).
struct PlacedToken: Equatable, Sendable {
    fileprivate init() {}
}

/// Where this copy of the app is, and whether it may act (decisions/0002 §3.3 and §13). Only the
/// copy at exactly /Applications/<name>.app, with the name its Info.plist gives and the case of
/// each letter as there, starts the host, and with it everything else the app does. Any other
/// copy says why it can't run and how to fix that, offers Quit, and starts nothing.
enum Placement: Equatable, Sendable {
    /// Where a copy that is not in the Applications folder is.
    enum Where: Equatable, Sendable {
        /// Under /Volumes: the disk image it came on, or another disk.
        case diskImage
        /// In a folder inside /Applications, such as Utilities.
        case insideApplications
        /// In the Applications folder in the home folder.
        case homeApplications
        /// Anywhere else: Downloads, the desktop, or a folder macOS moved it to before opening it.
        case elsewhere
    }

    case placed(PlacedToken)
    case notInApplications(Where)
    /// Directly in /Applications, under a name that is not the app's, such as the Finder's Keep
    /// Both gives a second copy ("<name> 2"). `copy` is that name, read from the folder, since Keep
    /// Both does not change the Info.plist; `originalPresent`, whether another copy of this app is
    /// at the app's own name.
    case renamed(copy: String, originalPresent: Bool)

    /// The one folder the app runs from.
    static let applications = "/Applications"
    /// The most characters of a copy's name a note shows.
    static let longestCopyName = 40

    /// This copy, judged once: from where it was opened, where that leads once links are
    /// followed, the name in its Info.plist (read here, not through `HostText.appName`, whose
    /// fallback is no name), the home folder, and whether another copy of this app is at the app's
    /// own name.
    static let current: Placement = {
        let bundle = Bundle.main.bundleURL
        let resolved = bundle.resolvingSymlinksInPath().path
        let name = Bundle.main.object(forInfoDictionaryKey: "CFBundleDisplayName") as? String
        var original = false
        if let name, !name.isEmpty {
            original = Placement.originalPresent(
                at: Placement.applications + "/" + name + ".app", runningAt: resolved,
                identifier: Bundle.main.bundleIdentifier
            )
        }
        return Placement.judge(
            bundlePath: bundle.path, resolvedPath: resolved, appName: name, home: NSHomeDirectory(),
            originalPresent: original
        )
    }()

    var isPlaced: Bool {
        if case .placed = self {
            return true
        }
        return false
    }

    /// Where a copy opened from `bundlePath`, which leads to `resolvedPath`, is. Placed only when
    /// `appName` is a name, and both paths are exactly /Applications/<appName>.app, case included.
    /// Otherwise the copy is judged by the path it was opened from, or, when that is the right one
    /// and only a link leads there, by where the link leads.
    static func judge(
        bundlePath: String, resolvedPath: String, appName: String?, home: String, originalPresent: Bool
    ) -> Placement {
        let opened = standardized(bundlePath)
        let resolved = standardized(resolvedPath)
        let name = appName ?? ""
        let right = applications + "/" + name + ".app"
        if !name.isEmpty && opened == right && resolved == right {
            return .placed(PlacedToken())
        }
        let found = opened == right ? resolved : opened
        if found == right {
            return .notInApplications(.elsewhere)
        }
        if (found as NSString).deletingLastPathComponent == applications {
            guard !name.isEmpty, let copy = copyName(of: found) else {
                return .notInApplications(.elsewhere)
            }
            return .renamed(copy: copy, originalPresent: originalPresent)
        }
        if found.hasPrefix("/Volumes/") {
            return .notInApplications(.diskImage)
        }
        if found.hasPrefix(applications + "/") {
            return .notInApplications(.insideApplications)
        }
        if found.hasPrefix(standardized(home) + "/Applications/") {
            return .notInApplications(.homeApplications)
        }
        return .notInApplications(.elsewhere)
    }

    /// Whether another copy of this app, not the one running at `running`, is at `expected`: both
    /// are folders, links followed, they are different items (device and inode), and the Info.plist
    /// of the one at `expected` gives `identifier`, the running copy's bundle identifier. False
    /// whenever any of that cannot be read, so that a note never tells a user to put their only
    /// copy in the Trash: an empty folder, one with no Info.plist, or another app at the app's name
    /// is not the original. On a disk that ignores case, the running copy spelled in another case
    /// is the same item, so it is not another copy.
    static func originalPresent(at expected: String, runningAt running: String, identifier: String?) -> Bool {
        guard let identifier, !identifier.isEmpty,
              let other = folderIdentity(expected), let this = folderIdentity(running), other != this
        else {
            return false
        }
        return Bundle(path: expected)?.bundleIdentifier == identifier
    }

    /// The name a copy's folder gives it, without ".app" and without its control, format, line
    /// and paragraph separator characters (a reordering mark, a zero-width space), which could turn
    /// the words around it or hide part of them; cut to `longestCopyName` characters. Nil when
    /// nothing is left.
    private static func copyName(of path: String) -> String? {
        let folder = ((path as NSString).lastPathComponent as NSString).deletingPathExtension
        var kept = ""
        for scalar in folder.unicodeScalars {
            switch scalar.properties.generalCategory {
            case .control, .format, .lineSeparator, .paragraphSeparator:
                continue
            default:
                kept.unicodeScalars.append(scalar)
            }
        }
        let shown = String(kept.prefix(longestCopyName))
        return shown.isEmpty ? nil : shown
    }

    /// The device and inode of the folder at `path`, links followed; nil when it is not a folder or
    /// cannot be read.
    private static func folderIdentity(_ path: String) -> [UInt64]? {
        let real = URL(fileURLWithPath: path).resolvingSymlinksInPath().path
        var isFolder: ObjCBool = false
        guard FileManager.default.fileExists(atPath: real, isDirectory: &isFolder), isFolder.boolValue,
              let attributes = try? FileManager.default.attributesOfItem(atPath: real),
              let device = attributes[.systemNumber] as? NSNumber,
              let inode = attributes[.systemFileNumber] as? NSNumber
        else {
            return nil
        }
        return [device.uint64Value, inode.uint64Value]
    }

    private static func standardized(_ path: String) -> String {
        let clean = (path as NSString).standardizingPath
        return clean.isEmpty ? path : clean
    }
}
