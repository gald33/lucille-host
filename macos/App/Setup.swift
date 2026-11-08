import Foundation

// Set Up (decisions/0002 §3.1 steps 8 to 10, §11.6): once a pair They Match confirmed in this app
// session is connected, one window with pages: Add back, only for a pairing whose own child set
// the policy aside; Folders; What Lucille may do; and Ready. Foundation only, so the self-test runs
// it without a screen.

/// The last policy a pairing set aside, as `policy previous --json` writes it (spec/CLI.md §6,
/// `previous_policy`), or that none was. Its state is closed within version 1, as an operation's
/// is (`HostObject.readPreviousPolicy`).
enum PreviousPolicy: Equatable, Sendable {
    /// No pairing has set a policy aside. Not `none`, which would read as `Optional.none`.
    case nothingSetAside
    /// When it was set aside, to the second, and the operations and folders it held.
    case found(setAsideAt: Date, ops: [String], folders: [Folder])
}

/// What the app asks before it adds a row back, as the menu asks before it adds the same thing:
/// the question about the home folder, before the run when the folder's path shows it is the home
/// folder or holds it, else once the host refuses it as `broad_folder`; and the acknowledgement
/// before it turns on an ability whose replies can carry a file's contents.
enum RestoreQuestion: Equatable, Sendable {
    case homeFolder
    case turnOn
}

/// One thing Set Up can add back: a folder, or an ability as `ops list` gives it now.
enum RestoreItem: Equatable, Sendable {
    case folder(Folder)
    case ability(Ability)
}

/// One row of the Add back page.
struct RestoreRow: Equatable, Sendable {
    let item: RestoreItem
    /// It has a box the user may check. The whole disk is shown with none, and the reason why.
    let offered: Bool
    /// What the app asks before it adds it, if anything.
    let asks: RestoreQuestion?
    /// What the row says, beside its name.
    let note: String?
}

/// What the Add back page offers: the set-aside policy's folders, then its operations, in its
/// order, each that the menu's own gates can add, every box unchecked.
struct RestoreOffer: Equatable, Sendable {
    let rows: [RestoreRow]
    /// Something the set-aside policy held is left out: it is already set up, or the app doesn't
    /// offer it here. The whole disk's rows are shown, so they do not count.
    let someNotOffered: Bool

    /// The offer, from the policy `previous` set aside, for a pairing this app session started at
    /// `start`, with `ops` and `roots` as `ops list` and `roots list` give them now. Nil when none
    /// was set aside, when it was set aside before the second the pairing started (another
    /// pairing's, spec/CLI.md §6), or when no row is left with a box.
    ///
    /// - A folder whose path holds U+FFFD is left out: the path can be shown but not passed back
    ///   (spec/CLI.md §4.1). So is one whose alias a folder already has.
    /// - The whole disk is shown with no box, and says why: the app never adds it. Every folder is
    ///   tested by its spelling (`HostText.namesWholeDisk`), which reads nothing, whatever the
    ///   set-aside policy marked it: `/System/Volumes/Data` holds the home folder, and so is stored
    ///   `broad`, only while the home folder is on that volume (policy.py). A folder marked `broad`
    ///   is also read from the disk (`HostText.isWholeDisk`), for another spelling of the whole
    ///   disk; an ordinary folder is not read at all. `HostModel.restore` refuses the whole disk by
    ///   its spelling again, before any run.
    /// - A `broad` folder, and one that is the home folder or holds it by its path, asks the home
    ///   question first: before the run when its path shows it, as the menu asks, else once the
    ///   host refuses it as `broad_folder`.
    /// - An operation is offered only when `ops list` has it off, plainly (`disabled`) and listed
    ///   as the menu lists it (`isShown`): not on already, not meant for testing, not
    ///   `host.describe`, not one this Mac can't run, not one this release lacks, and not one an
    ///   update turned off, whose Review and Turn On is not built (decisions/0002 §13). One whose
    ///   replies can carry a file's contents asks for the acknowledgement first.
    static func items(previous: PreviousPolicy, since start: Date, ops: [Ability], roots: [Folder], home: String) -> RestoreOffer? {
        guard case .found(let setAsideAt, let names, let folders) = previous else {
            return nil
        }
        let second = Date(timeIntervalSince1970: start.timeIntervalSince1970.rounded(.down))
        guard setAsideAt >= second else {
            return nil
        }
        let held = Set(roots.map(\.alias))
        var rows: [RestoreRow] = []
        var leftOut = false
        for folder in folders {
            if folder.path.unicodeScalars.contains("\u{FFFD}") || held.contains(folder.alias) {
                leftOut = true
                continue
            }
            if HostText.namesWholeDisk(folder.path) || (folder.broad && HostText.isWholeDisk(path: folder.path)) {
                rows.append(RestoreRow(item: .folder(folder), offered: false, asks: nil, note: SetupText.wholeDiskNotOffered))
                continue
            }
            let broad = folder.broad || HostText.broadFolder(path: folder.path, home: home) != nil
            rows.append(RestoreRow(item: .folder(folder), offered: true, asks: broad ? .homeFolder : nil, note: nil))
        }
        for name in names {
            guard let ability = ops.first(where: { $0.name == name }), ability.isShown, ability.state == .disabled else {
                leftOut = true
                continue
            }
            let asks: RestoreQuestion? = ability.asksFirst(turningOn: true) ? .turnOn : nil
            rows.append(RestoreRow(item: .ability(ability), offered: true, asks: asks, note: nil))
        }
        guard rows.contains(where: { $0.offered }) else {
            return nil
        }
        return RestoreOffer(rows: rows, someNotOffered: leftOut)
    }
}

extension Ability {
    /// Whether Set Up's What Lucille may do page has a switch for it: every operation but those
    /// meant for testing and a name this release does not have; `host.describe` too, as it is.
    var isOnSetUpPage: Bool {
        !diagnostic && state != .notInCatalog
    }
}

/// Set Up, as a value: inputs in, effects out, as `PairingFlow` is. It draws nothing and starts
/// nothing; the app's glue runs each effect and feeds back what came of it.
///
/// The rules it keeps, each a self-test check (`checkSetup`, `checkSetupWalk`):
/// - The window opens only the first time the status line says Connected after They Match was
///   clicked in this app session: never after a relaunch, nor for a pair confirmed in Terminal.
/// - `policy previous` is read only when this session's own pairing child, for the attempt They
///   Match confirmed, said it set the policy aside (`policy_reset`). A read that fails, or finds
///   nothing to offer, gives no Add back page.
/// - Nothing is added back but a row the user checked that has a box, each once, in order. Every
///   box starts unchecked, and there is no way to check them all at once.
/// - Ready's box, Open <name> when you log in, is checked each time Ready comes from What Lucille
///   may do. Only Done on Ready with it checked asks to register the login item
///   (`registerLoginItem`): not the box itself, and not the window closed on Ready.
struct SetupFlow: Equatable, Sendable {
    /// `policy previous` loads the policy set aside, which can wait on a stalled mount.
    static let previousLimit: TimeInterval = 30
    /// `roots check`, as adding a folder has: macOS may first ask the user about the folder.
    static let checkLimit: TimeInterval = 600

    enum Page: Equatable, Sendable {
        /// Add back what you had before? The rows checked so far.
        case addBack(RestoreOffer, checked: Set<Int>)
        /// The checked rows are being added back, in order: those left. A folder's row is done
        /// once the host has answered for it; an ability's once its switch has been turned, as the
        /// menu's is, before the host answers (`HostModel.restore`).
        case addingBack(RestoreOffer, left: [Int])
        case folders
        case abilities
        case ready
    }

    enum Effect: Equatable, Sendable {
        /// Run `policy previous --json`, then `ops list` and `roots list`, and send `previousRead`.
        case readPrevious
        /// Show the window on this page.
        case show(Page)
        /// Add this row back, through the menu's own gates, then send `itemDone` for it once
        /// `HostModel.restore` returns.
        case restore(row: Int, RestoreItem)
        /// A note: the policy set aside can't be read.
        case note(HostError)
        /// Ask macOS to open the app when the user logs in: Done, with Ready's box checked, once
        /// the window has closed.
        case registerLoginItem
        /// Close the window.
        case close
    }

    enum Input: Equatable, Sendable {
        /// This session's own pairing child started, at this time, in the pairing flow's epoch.
        case pairStarted(at: Date, epoch: Int)
        /// That child said it set the policy aside (`policy_reset`).
        case policySetAside(epoch: Int)
        /// They Match, clicked here, confirmed the pair, in the pairing flow's epoch.
        case confirmedHere(epoch: Int)
        /// The status line says Connected, or Connected, waiting for Lucille.
        case connected
        /// What `policy previous` gave, with the abilities and folders as they are now.
        case previousRead(VerbOutcome, ops: [Ability], roots: [Folder])
        /// The rows checked on the Add back page.
        case checked(Set<Int>)
        case addChecked
        case skip
        /// The row being added back is done, however it ended.
        case itemDone(row: Int)
        /// Continue.
        case next
        /// Ready's box, Open <name> when you log in, checked or not.
        case openAtLogin(Bool)
        case done
        /// The user closed the window.
        case windowClosed
        /// The menu's Set Up Folders and Abilities….
        case reopen
        /// The pair ended: a Disconnect, or the pair gone.
        case pairEnded
    }

    /// The home folder, for the question about it.
    let home: String
    /// The page the window shows, or showed when it was closed.
    private(set) var page: Page?
    private(set) var windowOpen = false
    /// Set Up is owed: They Match confirmed a pair in this session, and Done has not ended it.
    private(set) var owed = false
    private var startedAt: Date?
    private var startedEpoch: Int?
    private var setAside = false
    private var confirmedEpoch: Int?
    private var reading = false
    /// The window has opened since They Match last confirmed.
    private var opened = false
    /// Ready's box: checked each time Ready is shown from What Lucille may do.
    private(set) var openAtLogin = true

    init(home: String) {
        self.home = home
    }

    /// Whether the menu offers Set Up Folders and Abilities…: owed, opened once, closed before its
    /// last page (closed on Ready, Set Up is done, as Done makes it).
    var offersReopen: Bool {
        owed && opened && !windowOpen && !reading && page != nil
    }

    mutating func handle(_ input: Input) -> [Effect] {
        switch input {
        case .pairStarted(let at, let epoch):
            startedAt = at
            startedEpoch = epoch
            setAside = false
            return []
        case .policySetAside(let epoch):
            if epoch == startedEpoch {
                setAside = true
            }
            return []
        case .confirmedHere(let epoch):
            confirmedEpoch = epoch
            owed = true
            if !windowOpen {
                opened = false
            }
            return []
        case .connected:
            return connected()
        case .previousRead(let outcome, let ops, let roots):
            return previousRead(outcome, ops: ops, roots: roots)
        case .checked(let rows):
            return checked(rows)
        case .addChecked:
            return addChecked()
        case .skip:
            guard windowOpen, case .addBack? = page else {
                return []
            }
            return showPage(.folders)
        case .itemDone(let row):
            return itemDone(row)
        case .next:
            return next()
        case .openAtLogin(let on):
            guard windowOpen, page == .ready else {
                return []
            }
            openAtLogin = on
            return []
        case .done:
            guard windowOpen, page == .ready else {
                return []
            }
            owed = false
            windowOpen = false
            page = nil
            return openAtLogin ? [.close, .registerLoginItem] : [.close]
        case .windowClosed:
            guard windowOpen else {
                return []
            }
            windowOpen = false
            if page == .ready {
                // Closed on Ready: done, as Done is.
                owed = false
                page = nil
            }
            return []
        case .reopen:
            guard offersReopen, let shown = page else {
                return []
            }
            windowOpen = true
            return [.show(shown)]
        case .pairEnded:
            let wasOpen = windowOpen
            self = SetupFlow(home: home)
            return wasOpen ? [.close] : []
        }
    }

    private mutating func showPage(_ shown: Page) -> [Effect] {
        page = shown
        windowOpen = true
        return [.show(shown)]
    }

    private mutating func connected() -> [Effect] {
        guard owed, !opened, !reading else {
            return []
        }
        opened = true
        if setAside, let started = startedEpoch, started == confirmedEpoch, startedAt != nil {
            reading = true
            return [.readPrevious]
        }
        return showPage(.folders)
    }

    private mutating func previousRead(_ outcome: VerbOutcome, ops: [Ability], roots: [Folder]) -> [Effect] {
        guard reading else {
            return []
        }
        reading = false
        switch outcome {
        case .done:
            if let previous = outcome.previousPolicy, let start = startedAt,
               let offer = RestoreOffer.items(previous: previous, since: start, ops: ops, roots: roots, home: home) {
                return showPage(.addBack(offer, checked: []))
            }
            return showPage(.folders)
        case .refused(let error) where error.code == "previous_invalid":
            var effects = showPage(.folders)
            effects.append(.note(error))
            return effects
        default:
            return showPage(.folders)
        }
    }

    private mutating func checked(_ rows: Set<Int>) -> [Effect] {
        guard windowOpen, case .addBack(let offer, _)? = page else {
            return []
        }
        let kept = rows.filter { $0 >= 0 && $0 < offer.rows.count && offer.rows[$0].offered }
        let shown = Page.addBack(offer, checked: kept)
        page = shown
        return [.show(shown)]
    }

    private mutating func addChecked() -> [Effect] {
        guard windowOpen, case .addBack(let offer, let checked)? = page, !checked.isEmpty else {
            return []
        }
        let left = checked.sorted()
        let shown = Page.addingBack(offer, left: left)
        page = shown
        return [.show(shown), .restore(row: left[0], offer.rows[left[0]].item)]
    }

    private mutating func itemDone(_ row: Int) -> [Effect] {
        guard case .addingBack(let offer, let left)? = page, left.first == row else {
            return []
        }
        let rest = Array(left.dropFirst())
        guard let following = rest.first else {
            page = .folders
            return windowOpen ? [.show(.folders)] : []
        }
        let shown = Page.addingBack(offer, left: rest)
        page = shown
        var effects: [Effect] = windowOpen ? [.show(shown)] : []
        effects.append(.restore(row: following, offer.rows[following].item))
        return effects
    }

    private mutating func next() -> [Effect] {
        guard windowOpen else {
            return []
        }
        switch page {
        case .folders?:
            return showPage(.abilities)
        case .abilities?:
            openAtLogin = true
            return showPage(.ready)
        default:
            return []
        }
    }
}

/// Set Up's words, page by page. The app's name is read from its Info.plist (`HostText.appName`).
enum SetupText {
    private static let appName = HostText.appName

    /// The menu's item while Set Up is owed and its window is closed.
    static let menuItem = "Set Up Folders and Abilities…"
    static let windowTitle = "Set Up \(appName)"

    // The Add back page.
    static let addBackTitle = "Add back what you had before?"
    static let addBackText = "Connecting started \(appName)'s settings afresh. These are the folders and abilities you had before. Check the ones to add back. Nothing is added unless you check it."
    /// On only while something is checked.
    static let addCheckedButton = "Add Checked"
    /// The default.
    static let skipButton = "Skip"
    static let someNotOffered = "Some of what you had before isn't shown: it is already set up, or \(appName) doesn't offer it here."
    static let wholeDiskNotOffered = "\(appName) doesn't add your whole disk back, so it isn't offered."
    static let asksHomeFolder = "This is your home folder, or holds it. \(appName) asks before it adds it back."
    static let asksTurnOn = "Its replies can carry a file's contents. \(appName) asks before it turns it on."
    static let addingBack = "Adding back what you checked…"
    /// The note's title when the policy set aside can't be read (`previous_invalid`), whose words
    /// are `HostText.refusal`'s.
    static let previousUnreadableTitle = "Couldn't read what you had before"

    // The Folders page.
    static let foldersTitle = "Folders Lucille may use"
    static let noFolders = "No folders yet"
    static let foldersUnread = "Couldn't read your folders"
    static let addFolderButton = "Add a Folder…"
    static let removeButton = "Remove"
    static let continueButton = "Continue"
    /// Words only: Open Privacy Settings comes with the app's one opener.
    static let folderBlocked = "macOS is blocking \(appName) from this folder."
    static let folderDenied = "This folder's permissions don't let \(appName) open it."
    static let folderMissing = "This folder isn't there any more."
    static let folderUnopened = "\(appName) couldn't open this folder."

    // The What Lucille may do page.
    static let abilitiesTitle = "What Lucille may do"
    static let abilitiesUnread = "Couldn't read abilities"
    static let noAbilities = "None to show"

    // The Ready page.
    static let readyTitle = "\(appName) is ready"
    static let readyText = "Lucille can now ask this Mac to do what is turned on, in the folders you added."
    /// Checked when Ready comes. With it checked, Done asks macOS to open the app at login, and
    /// macOS shows its own notice, which the line under it announces (decisions/0002 §3.1 step 10).
    static let openAtLoginBox = "Open \(appName) when you log in"
    static let openAtLoginNotice = "macOS will show a notice that \(appName) can now open when you log in. That is expected. You can change it later in System Settings › General › Login Items & Extensions."
    static let doneButton = "Done"

    /// What the Folders page says under a folder, from `roots check` (spec/CLI.md §9.2): nothing
    /// for a folder the host can open.
    static func folderState(_ state: FolderCheck.State) -> String? {
        switch state {
        case .ok:
            return nil
        case .deniedByMacOS:
            return folderBlocked
        case .denied:
            return folderDenied
        case .missing:
            return folderMissing
        case .error:
            return folderUnopened
        }
    }

    /// What a row of the Add back page is called: a folder by the name Lucille knows it by, an
    /// ability in the core's words.
    static func rowTitle(_ item: RestoreItem) -> String {
        switch item {
        case .folder(let folder):
            return "\u{201C}\(folder.alias)\u{201D}"
        case .ability(let ability):
            return ability.title
        }
    }

    /// What a row says under its name, one line each: where a folder is; then why it has no box,
    /// or what the app asks before it adds it back; and for an ability that asks nothing, what
    /// Lucille gets, in the core's words.
    static func rowDetail(_ row: RestoreRow) -> String? {
        var lines: [String] = []
        if case .folder(let folder) = row.item {
            lines.append(folder.path)
        }
        if let note = row.note {
            lines.append(note)
        } else if let asks = row.asks {
            lines.append(asks == .homeFolder ? asksHomeFolder : asksTurnOn)
        } else if case .ability(let ability) = row.item, let sends = ability.detail {
            lines.append(sends)
        }
        return lines.isEmpty ? nil : lines.joined(separator: "\n")
    }
}
