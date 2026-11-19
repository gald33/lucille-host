import AppKit

/// The Gmail window, from the menu's Gmail… item (decisions/0003, "Second use"): the Gmail
/// address, the app password in a field that does not show it, Save, Test and Remove, and a
/// button that opens Google's App Passwords page. It says nothing when an app password is saved
/// beyond that it was. It decides nothing: each button calls the closure `HostModel` sets, which
/// runs the host's `credential` verb. The app password goes only to that verb's standard input,
/// never into its arguments or any log, and the field, its field editor and its undo are emptied
/// once Save has taken it and whenever the window closes.
@MainActor
final class GmailPanel: NSObject, NSWindowDelegate {
    /// Save: the address and the app password as typed.
    var onSave: ((String, String) -> Void)?
    var onTest: (() -> Void)?
    var onRemove: (() -> Void)?
    var onOpenPage: (() -> Void)?

    private let width: CGFloat = 520
    private let panel: GatePanel
    private let address: NSTextField
    private let password: NSSecureTextField
    private let kept: NSTextField
    private let said: NSTextField
    private var buttons: [NSButton] = []
    /// How many verbs are running: the buttons wait until none is.
    private var busyCount = 0
    private var content: NSView

    override init() {
        panel = GatePanel(
            contentRect: NSRect(x: 0, y: 0, width: 520, height: 300),
            styleMask: [.titled, .closable],
            backing: .buffered,
            defer: true
        )
        address = NSTextField(string: "")
        password = NSSecureTextField(string: "")
        kept = NSTextField(wrappingLabelWithString: "")
        said = NSTextField(wrappingLabelWithString: "")
        content = NSView()
        super.init()
        panel.isReleasedWhenClosed = false
        panel.hidesOnDeactivate = false
        // Not restorable: macOS would keep its state in a folder named after the app.
        panel.isRestorable = false
        panel.title = GmailText.title
        panel.onCloseAsked = { [weak self] in
            self?.hide()
        }
        panel.delegate = self
        build()
    }

    var isVisible: Bool { panel.isVisible }

    /// Show the window, at the front, its focus in the address field when it was hidden.
    func show() {
        if !panel.isVisible {
            said.stringValue = ""
            fit()
            panel.center()
            panel.initialFirstResponder = address
            NSApp.activate()
            panel.makeKeyAndOrderFront(nil)
        }
        panel.orderFrontRegardless()
    }

    func hide() {
        clearPassword()
        panel.orderOut(nil)
    }

    /// What the window says of the app password kept on this Mac.
    func setKept(_ saved: SavedCredential?) {
        kept.stringValue = GmailText.kept(saved)
        fit()
    }

    /// The window's one line of news: what the last button did.
    func say(_ text: String) {
        said.stringValue = text
        fit()
    }

    /// Whether a verb the window started is running.
    var isBusy: Bool { busyCount > 0 }

    /// While a verb runs, the buttons wait, and the window says what is happening. The calls are
    /// counted, so one verb that ends while another runs (a read when the window is opened again
    /// during a Save) leaves the buttons waiting.
    func setBusy(_ busy: Bool, saying text: String? = nil) {
        busyCount = max(0, busyCount + (busy ? 1 : -1))
        for button in buttons {
            button.isEnabled = busyCount == 0
        }
        if let text {
            say(text)
        }
    }

    /// Empty the app password's field, the editor that edits it while it has the focus, the
    /// window's shared field editor, and their undo.
    func clearPassword() {
        if let editor = password.currentEditor() {
            editor.string = ""
            editor.undoManager?.removeAllActions()
        }
        panel.endEditing(for: nil)
        password.stringValue = ""
        panel.fieldEditor(false, for: nil)?.string = ""
        panel.undoManager?.removeAllActions()
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        hide()
        return false
    }

    // MARK: The page

    private func build() {
        let intro = label(GmailText.intro)
        let howTo = label(GmailText.howTo, secondary: true)
        let open = NSButton(title: GmailText.openPageButton, target: self, action: #selector(openPagePressed(_:)))
        address.placeholderString = GmailText.addressPlaceholder
        address.setAccessibilityLabel(GmailText.addressLabel)
        password.placeholderString = GmailText.passwordPlaceholder
        password.setAccessibilityLabel(GmailText.passwordLabel)
        for field in [address, password] {
            field.usesSingleLineMode = true
            field.cell?.wraps = false
            field.cell?.isScrollable = true
        }
        kept.preferredMaxLayoutWidth = width - 40
        said.preferredMaxLayoutWidth = width - 40
        let remove = NSButton(title: GmailText.removeButton, target: self, action: #selector(removePressed(_:)))
        let test = NSButton(title: GmailText.testButton, target: self, action: #selector(testPressed(_:)))
        let save = NSButton(title: GmailText.saveButton, target: self, action: #selector(savePressed(_:)))
        save.keyEquivalent = "\r"
        buttons = [open, remove, test, save]
        content = layout(
            [
                intro, howTo, leading(open), kept,
                row(GmailText.addressLabel, address), row(GmailText.passwordLabel, password), said,
            ],
            buttons: [remove, test, save]
        )
        panel.contentView = content
    }

    private func label(_ text: String, secondary: Bool = false) -> NSTextField {
        let made = NSTextField(wrappingLabelWithString: text)
        made.preferredMaxLayoutWidth = width - 40
        if secondary {
            made.textColor = .secondaryLabelColor
        }
        return made
    }

    /// `view` at the left of a row of its own.
    private func leading(_ view: NSView) -> NSView {
        let holder = NSView()
        view.translatesAutoresizingMaskIntoConstraints = false
        holder.addSubview(view)
        NSLayoutConstraint.activate([
            view.topAnchor.constraint(equalTo: holder.topAnchor),
            view.bottomAnchor.constraint(equalTo: holder.bottomAnchor),
            view.leadingAnchor.constraint(equalTo: holder.leadingAnchor),
            view.trailingAnchor.constraint(lessThanOrEqualTo: holder.trailingAnchor),
        ])
        return holder
    }

    /// A field with its name at its left.
    private func row(_ name: String, _ field: NSTextField) -> NSView {
        let holder = NSView()
        let title = NSTextField(labelWithString: name)
        for child in [title, field] {
            child.translatesAutoresizingMaskIntoConstraints = false
            holder.addSubview(child)
        }
        NSLayoutConstraint.activate([
            title.leadingAnchor.constraint(equalTo: holder.leadingAnchor),
            title.widthAnchor.constraint(equalToConstant: 110),
            title.firstBaselineAnchor.constraint(equalTo: field.firstBaselineAnchor),
            field.leadingAnchor.constraint(equalTo: title.trailingAnchor, constant: 8),
            field.trailingAnchor.constraint(equalTo: holder.trailingAnchor),
            field.topAnchor.constraint(equalTo: holder.topAnchor),
            field.bottomAnchor.constraint(equalTo: holder.bottomAnchor),
        ])
        return holder
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
        NSLayoutConstraint.activate(constraints)
        return view
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

    @objc private func savePressed(_ sender: NSButton) {
        panel.endEditing(for: nil)
        onSave?(address.stringValue, password.stringValue)
    }

    @objc private func testPressed(_ sender: NSButton) {
        onTest?()
    }

    @objc private func removePressed(_ sender: NSButton) {
        onRemove?()
    }

    @objc private func openPagePressed(_ sender: NSButton) {
        onOpenPage?()
    }
}
