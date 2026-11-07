import AppKit

/// Set Up's window (decisions/0002 §3.1 steps 8 to 10): Add back what you had before?, the
/// folders Lucille may use, what Lucille may do, and Ready, one page at a time, as `SetupFlow`
/// says. It draws the page it is given, with the folders, their checks and the abilities as the
/// menu last read them, and tells the app what the user did; it decides nothing and runs nothing
/// itself.
///
/// - It is one non-modal window that stays on the screen while the user works in another app. It
///   is not restorable and keeps no frame of its own: macOS would otherwise keep the window's state
///   in a folder named after the app, and a frame is a setting.
/// - Every box on the Add back page starts unchecked, and only the user's click checks one. Add
///   Checked is on only while one is checked; Skip is the default.
/// - A switch on What Lucille may do shows what the host last said. A click asks for the change,
///   which the menu makes through its own gates, and the switch then follows the host's answer.
/// - Its views sit straight in the window, in no group, as in a note.
@MainActor
final class SetupPanel: NSObject, NSWindowDelegate {
    /// The rows checked on the Add back page, after a click on a box.
    var onChecked: ((Set<Int>) -> Void)?
    var onAddChecked: (() -> Void)?
    var onSkip: (() -> Void)?
    /// Continue, on the Folders page and on What Lucille may do.
    var onNext: (() -> Void)?
    var onDone: (() -> Void)?
    /// The user closed the window.
    var onClosed: (() -> Void)?
    var onAddFolder: (() -> Void)?
    var onRemoveFolder: ((Folder) -> Void)?
    /// A switch was clicked: the ability as the window shows it, and the position asked for.
    var onSwitch: ((Ability, Bool) -> Void)?

    /// The menu's set-up as it last read it, which the Folders page and What Lucille may do show.
    struct Shown: Equatable {
        var folders: [Folder]
        /// False when `roots list` failed, so the page does not claim there are none.
        var foldersRead: Bool
        var checks: [FolderCheck]
        var abilities: [Ability]
        /// False when `ops list` failed.
        var abilitiesRead: Bool
    }

    /// One switch of What Lucille may do, with the ability it shows.
    private struct Switch {
        let button: NSButton
        var ability: Ability
    }

    private let width: CGFloat = 480
    private let panel: NSPanel
    private var page: SetupFlow.Page?
    private var content: NSView
    private var shown = Shown(folders: [], foldersRead: true, checks: [], abilities: [], abilitiesRead: true)
    /// The Add back page's boxes, by row.
    private var boxes: [Int: NSButton] = [:]
    private var addChecked: NSButton?
    private var switches: [Switch] = []
    /// The folders the Folders page shows, in the order of their Remove buttons' tags.
    private var removable: [Folder] = []

    override init() {
        panel = NSPanel(
            contentRect: NSRect(x: 0, y: 0, width: 480, height: 200),
            styleMask: [.titled, .closable],
            backing: .buffered,
            defer: true
        )
        content = NSView()
        super.init()
        panel.isReleasedWhenClosed = false
        panel.hidesOnDeactivate = false
        // Not restorable: macOS would keep its state in a folder named after the app.
        panel.isRestorable = false
        panel.title = SetupText.windowTitle
        panel.delegate = self
    }

    /// Show the window on `next`, with the menu's set-up as it last read it. On the Add back page,
    /// a click on a box changes only which boxes are checked.
    func show(_ next: SetupFlow.Page, with read: Shown) {
        shown = read
        if case .addBack(let offer, let checked) = next, case .addBack(let current, _)? = page, offer == current {
            page = next
            setChecked(checked)
        } else {
            page = next
            draw()
        }
        present()
    }

    /// The menu's set-up as it was read again. The Folders page is drawn again if it changed; the
    /// switches of What Lucille may do are set to what the host said, and the page is drawn again
    /// if an ability came or went, or says something else.
    func update(_ read: Shown) {
        let foldersChanged = read.folders != shown.folders || read.foldersRead != shown.foldersRead
            || read.checks != shown.checks
        let abilitiesUnread = read.abilitiesRead != shown.abilitiesRead
        shown = read
        switch page {
        case .folders?:
            if foldersChanged {
                draw()
            }
        case .abilities?:
            let drawn = switches.map { SetupPanel.shape(of: $0.ability) }
            let now = read.abilities.filter(\.isOnSetUpPage).map { SetupPanel.shape(of: $0) }
            if drawn == now && !abilitiesUnread {
                setSwitches()
            } else {
                draw()
            }
        case .addBack?, .addingBack?, .ready?, nil:
            break
        }
    }

    /// Close the window, as Done does: no `onClosed`.
    func close() {
        guard page != nil else {
            return
        }
        page = nil
        panel.orderOut(nil)
    }

    // MARK: Pages

    private func draw() {
        boxes = [:]
        addChecked = nil
        switches = []
        removable = []
        let view: NSView
        switch page {
        case .addBack(let offer, let checked)?:
            view = addBackPage(offer, checked: checked)
        case .addingBack?:
            view = addingBackPage()
        case .folders?:
            view = foldersPage()
        case .abilities?:
            view = abilitiesPage()
        case .ready?:
            view = readyPage()
        case nil:
            return
        }
        panel.contentView = view
        content = view
        fit()
    }

    private func addBackPage(_ offer: RestoreOffer, checked: Set<Int>) -> NSView {
        var rows: [NSView] = [title(SetupText.addBackTitle), label(SetupText.addBackText)]
        for (index, row) in offer.rows.enumerated() {
            if row.offered {
                let box = NSButton(checkboxWithTitle: SetupText.rowTitle(row.item), target: self, action: #selector(boxChanged(_:)))
                box.tag = index
                box.state = checked.contains(index) ? .on : .off
                box.lineBreakMode = .byTruncatingMiddle
                boxes[index] = box
                rows.append(box)
            } else {
                rows.append(label(SetupText.rowTitle(row.item)))
            }
            if let detail = SetupText.rowDetail(row) {
                rows.append(label(detail, secondary: true))
            }
        }
        if offer.someNotOffered {
            rows.append(label(SetupText.someNotOffered, secondary: true))
        }
        let add = NSButton(title: SetupText.addCheckedButton, target: self, action: #selector(addCheckedPressed(_:)))
        add.isEnabled = !checked.isEmpty
        addChecked = add
        let skip = NSButton(title: SetupText.skipButton, target: self, action: #selector(skipPressed(_:)))
        skip.keyEquivalent = "\r"
        return layout(rows, buttons: [add, skip])
    }

    private func addingBackPage() -> NSView {
        let row = NSView()
        let spinner = NSProgressIndicator()
        spinner.style = .spinning
        spinner.controlSize = .small
        spinner.isIndeterminate = true
        spinner.startAnimation(nil)
        let text = NSTextField(labelWithString: SetupText.addingBack)
        for view in [spinner, text] {
            view.translatesAutoresizingMaskIntoConstraints = false
            row.addSubview(view)
        }
        NSLayoutConstraint.activate([
            spinner.leadingAnchor.constraint(equalTo: row.leadingAnchor),
            spinner.centerYAnchor.constraint(equalTo: text.centerYAnchor),
            text.leadingAnchor.constraint(equalTo: spinner.trailingAnchor, constant: 10),
            text.trailingAnchor.constraint(lessThanOrEqualTo: row.trailingAnchor),
            text.topAnchor.constraint(equalTo: row.topAnchor),
            text.bottomAnchor.constraint(equalTo: row.bottomAnchor),
        ])
        return layout([title(SetupText.addBackTitle), row], buttons: [])
    }

    private func foldersPage() -> NSView {
        var rows: [NSView] = [title(SetupText.foldersTitle)]
        if !shown.foldersRead {
            rows.append(label(SetupText.foldersUnread, secondary: true))
        } else if shown.folders.isEmpty {
            rows.append(label(SetupText.noFolders, secondary: true))
        }
        for folder in shown.folders {
            rows.append(folderRow(folder, tag: removable.count))
            removable.append(folder)
        }
        let add = NSButton(title: SetupText.addFolderButton, target: self, action: #selector(addFolderPressed(_:)))
        let next = NSButton(title: SetupText.continueButton, target: self, action: #selector(nextPressed(_:)))
        next.keyEquivalent = "\r"
        return layout(rows, buttons: [add, next])
    }

    /// One folder: the name Lucille knows it by, where it is, what `roots check` said of it if that
    /// is anything but that the host can open it, and Remove.
    private func folderRow(_ folder: Folder, tag: Int) -> NSView {
        let row = NSView()
        let buttonRoom: CGFloat = 110
        let name = label("\u{201C}\(folder.alias)\u{201D}", width: width - 40 - buttonRoom)
        var lines = [folder.path]
        let check = shown.checks.first(where: { $0.alias == folder.alias && $0.path == folder.path })
        if let check, let said = SetupText.folderState(check.state) {
            lines.append(said)
        }
        let detail = label(lines.joined(separator: "\n"), secondary: true, width: width - 40 - buttonRoom)
        let remove = NSButton(title: SetupText.removeButton, target: self, action: #selector(removePressed(_:)))
        remove.tag = tag
        for view in [name, detail, remove] {
            view.translatesAutoresizingMaskIntoConstraints = false
            row.addSubview(view)
        }
        NSLayoutConstraint.activate([
            name.topAnchor.constraint(equalTo: row.topAnchor),
            name.leadingAnchor.constraint(equalTo: row.leadingAnchor),
            name.trailingAnchor.constraint(lessThanOrEqualTo: remove.leadingAnchor, constant: -12),
            detail.topAnchor.constraint(equalTo: name.bottomAnchor, constant: 4),
            detail.leadingAnchor.constraint(equalTo: row.leadingAnchor),
            detail.trailingAnchor.constraint(lessThanOrEqualTo: remove.leadingAnchor, constant: -12),
            detail.bottomAnchor.constraint(equalTo: row.bottomAnchor),
            remove.trailingAnchor.constraint(equalTo: row.trailingAnchor),
            remove.centerYAnchor.constraint(equalTo: row.centerYAnchor),
        ])
        return row
    }

    /// A switch for each operation but those meant for testing and a name this release lacks,
    /// `host.describe` as it is: its position from the host's `enabled`, its words the core's,
    /// off when this Mac can't run it, with the sentence that says a state that is more than on or
    /// off, such as one an update turned off.
    private func abilitiesPage() -> NSView {
        var rows: [NSView] = [title(SetupText.abilitiesTitle)]
        let listed = shown.abilities.filter(\.isOnSetUpPage)
        if !shown.abilitiesRead {
            rows.append(label(SetupText.abilitiesUnread, secondary: true))
        } else if listed.isEmpty {
            rows.append(label(SetupText.noAbilities, secondary: true))
        }
        for ability in listed {
            let box = NSButton(checkboxWithTitle: ability.title, target: self, action: #selector(switchChanged(_:)))
            box.tag = switches.count
            box.state = ability.isOn ? .on : .off
            box.isEnabled = ability.canChange
            switches.append(Switch(button: box, ability: ability))
            rows.append(box)
            let lines = [ability.detail, ability.note].compactMap { $0 }
            if !lines.isEmpty {
                rows.append(label(lines.joined(separator: " "), secondary: true))
            }
        }
        let next = NSButton(title: SetupText.continueButton, target: self, action: #selector(nextPressed(_:)))
        next.keyEquivalent = "\r"
        return layout(rows, buttons: [next])
    }

    private func readyPage() -> NSView {
        let done = NSButton(title: SetupText.doneButton, target: self, action: #selector(donePressed(_:)))
        done.keyEquivalent = "\r"
        return layout([title(SetupText.readyTitle), label(SetupText.readyText)], buttons: [done])
    }

    /// What a switch shows of an ability, apart from its position: a switch is set again in place
    /// while these stay the same.
    private static func shape(of ability: Ability) -> [String] {
        [ability.name, ability.title, ability.detail ?? "", ability.note ?? "", ability.canChange ? "can" : "cannot"]
    }

    private func setSwitches() {
        for index in switches.indices {
            guard let now = shown.abilities.first(where: { $0.name == switches[index].ability.name }) else {
                continue
            }
            switches[index].ability = now
            switches[index].button.state = now.isOn ? .on : .off
            switches[index].button.isEnabled = now.canChange
        }
    }

    private func setChecked(_ checked: Set<Int>) {
        for (row, box) in boxes {
            box.state = checked.contains(row) ? .on : .off
        }
        addChecked?.isEnabled = !checked.isEmpty
    }

    private func title(_ text: String) -> NSTextField {
        let made = label(text)
        made.font = .boldSystemFont(ofSize: NSFont.systemFontSize)
        return made
    }

    private func label(_ text: String, secondary: Bool = false, width shownWidth: CGFloat? = nil) -> NSTextField {
        let made = NSTextField(wrappingLabelWithString: text)
        made.isSelectable = false
        made.preferredMaxLayoutWidth = shownWidth ?? width - 40
        if secondary {
            made.textColor = .secondaryLabelColor
        }
        return made
    }

    /// `rows` one under the other, full width, then `buttons` in one row at the right, the last
    /// one rightmost.
    private func layout(_ rows: [NSView], buttons: [NSButton]) -> NSView {
        let view = NSView()
        for child in rows + buttons {
            child.translatesAutoresizingMaskIntoConstraints = false
            view.addSubview(child)
        }
        var constraints = [view.widthAnchor.constraint(equalToConstant: width)]
        var above = view.topAnchor
        var gap: CGFloat = 20
        for row in rows {
            constraints += [
                row.topAnchor.constraint(equalTo: above, constant: gap),
                row.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: 20),
                row.trailingAnchor.constraint(equalTo: view.trailingAnchor, constant: -20),
            ]
            above = row.bottomAnchor
            gap = 12
        }
        var right = view.trailingAnchor
        var inset: CGFloat = -20
        for button in buttons.reversed() {
            constraints += [
                button.topAnchor.constraint(equalTo: above, constant: 20),
                button.trailingAnchor.constraint(equalTo: right, constant: inset),
                button.bottomAnchor.constraint(equalTo: view.bottomAnchor, constant: -20),
            ]
            right = button.leadingAnchor
            inset = -12
        }
        if buttons.isEmpty {
            constraints.append(above.constraint(equalTo: view.bottomAnchor, constant: -20))
        }
        NSLayoutConstraint.activate(constraints)
        return view
    }

    /// Show the window. It takes the focus only when it was hidden, so a page that changes while
    /// the user works in another app leaves the focus there.
    private func present() {
        guard !panel.isVisible else {
            return
        }
        panel.center()
        NSApp.activate()
        panel.makeKeyAndOrderFront(nil)
        panel.orderFrontRegardless()
    }

    /// Size the window to its page, keeping its top left corner where it is while it shows.
    private func fit() {
        content.layoutSubtreeIfNeeded()
        let height = content.fittingSize.height
        let top = panel.frame.maxY
        let left = panel.frame.minX
        panel.setContentSize(NSSize(width: width, height: height))
        if panel.isVisible {
            panel.setFrameTopLeftPoint(NSPoint(x: left, y: top))
        }
    }

    // MARK: What the user does

    @objc private func boxChanged(_ sender: NSButton) {
        var rows: Set<Int> = []
        for (row, box) in boxes where box.state == .on {
            rows.insert(row)
        }
        onChecked?(rows)
    }

    @objc private func addCheckedPressed(_ sender: Any?) {
        onAddChecked?()
    }

    @objc private func skipPressed(_ sender: Any?) {
        onSkip?()
    }

    @objc private func nextPressed(_ sender: Any?) {
        onNext?()
    }

    @objc private func donePressed(_ sender: Any?) {
        onDone?()
    }

    @objc private func addFolderPressed(_ sender: Any?) {
        onAddFolder?()
    }

    @objc private func removePressed(_ sender: NSButton) {
        guard removable.indices.contains(sender.tag) else {
            return
        }
        onRemoveFolder?(removable[sender.tag])
    }

    @objc private func switchChanged(_ sender: NSButton) {
        guard switches.indices.contains(sender.tag) else {
            return
        }
        onSwitch?(switches[sender.tag].ability, sender.state == .on)
    }

    // MARK: Closing the window

    /// The title bar's close button, or any other close that was not the app's own: tell the flow,
    /// which keeps the page for the menu's Set Up Folders and Abilities….
    func windowWillClose(_ notification: Notification) {
        guard page != nil else {
            return
        }
        page = nil
        onClosed?()
    }
}
