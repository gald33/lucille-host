import AppKit

/// A small window that says what the app is waiting for, with a spinner and a Cancel button.
/// Cancel closes it at once and calls `onCancel`; the caller stops the work.
@MainActor
final class ProgressPanel: NSObject {
    private let panel: NSPanel
    private let onCancel: () -> Void

    init(message: String, onCancel: @escaping () -> Void) {
        self.onCancel = onCancel
        panel = NSPanel(
            contentRect: NSRect(x: 0, y: 0, width: 380, height: 120),
            styleMask: [.titled],
            backing: .buffered,
            defer: true
        )
        super.init()
        panel.title = "Lucille Host"
        panel.isReleasedWhenClosed = false
        panel.level = .floating

        let spinner = NSProgressIndicator()
        spinner.style = .spinning
        spinner.controlSize = .small
        spinner.isIndeterminate = true
        spinner.startAnimation(nil)
        let label = NSTextField(wrappingLabelWithString: message)
        label.preferredMaxLayoutWidth = 300
        let button = NSButton(title: "Cancel", target: self, action: #selector(cancel(_:)))
        button.keyEquivalent = "\u{1b}"

        let row = NSStackView(views: [spinner, label])
        row.orientation = .horizontal
        row.alignment = .centerY
        row.spacing = 10
        let column = NSStackView(views: [row, button])
        column.orientation = .vertical
        column.alignment = .trailing
        column.spacing = 16
        column.edgeInsets = NSEdgeInsets(top: 20, left: 20, bottom: 20, right: 20)
        column.translatesAutoresizingMaskIntoConstraints = false
        let content = NSView()
        content.addSubview(column)
        NSLayoutConstraint.activate([
            column.leadingAnchor.constraint(equalTo: content.leadingAnchor),
            column.trailingAnchor.constraint(equalTo: content.trailingAnchor),
            column.topAnchor.constraint(equalTo: content.topAnchor),
            column.bottomAnchor.constraint(equalTo: content.bottomAnchor),
        ])
        panel.contentView = content
    }

    func show() {
        panel.center()
        NSApp.activate()
        panel.makeKeyAndOrderFront(nil)
    }

    func close() {
        panel.orderOut(nil)
    }

    @objc private func cancel(_ sender: Any?) {
        close()
        onCancel()
    }
}
