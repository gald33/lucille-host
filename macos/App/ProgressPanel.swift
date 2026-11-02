import AppKit

/// A small window that says what the app is waiting for, with a spinner, an optional second line
/// and an optional button: Cancel, or another title such as the finishing panel's Stop now. Cancel
/// closes the window at once; any button calls `onCancel`, and the caller does the rest.
///
/// It stays on the screen while the user works in another app (an `NSPanel` would otherwise hide
/// itself then), and its views sit straight in the window, in no group, so System Events finds its
/// button and its words where it finds those of a note.
@MainActor
final class ProgressPanel: NSObject {
    private let panel: NSPanel
    private let label: NSTextField
    private let button: NSButton?
    private let closesOnButton: Bool
    private let onButton: () -> Void
    private let width: CGFloat
    private let content: NSView

    /// `buttonTitle` nil shows no button. Escape presses the button only when it is Cancel.
    init(
        message: String,
        detail: String? = nil,
        buttonTitle: String? = ConnectionText.cancelButton,
        onCancel: @escaping () -> Void = {}
    ) {
        onButton = onCancel
        closesOnButton = buttonTitle == ConnectionText.cancelButton
        let made = buttonTitle.map { NSButton(title: $0, target: nil, action: nil) }
        button = made
        // Wide enough for the longest button, such as the finishing panel's Stop now.
        width = max(380, (made?.fittingSize.width ?? 0) + 40)
        panel = NSPanel(
            contentRect: NSRect(x: 0, y: 0, width: width, height: 120),
            styleMask: [.titled],
            backing: .buffered,
            defer: true
        )
        label = NSTextField(wrappingLabelWithString: message)
        content = NSView()
        super.init()
        panel.title = HostText.appName
        panel.isReleasedWhenClosed = false
        panel.level = .floating
        panel.hidesOnDeactivate = false
        // Its button works while the app waits to quit, when AppKit runs the main loop as for a
        // modal panel, and while a note is up. What the button starts goes on too: every modal
        // loop starts in a `RunLoopTurn`, never inside a main-queue callout.
        panel.worksWhenModal = true

        let spinner = NSProgressIndicator()
        spinner.style = .spinning
        spinner.controlSize = .small
        spinner.isIndeterminate = true
        spinner.startAnimation(nil)
        let textWidth = width - 70
        label.preferredMaxLayoutWidth = textWidth
        var views: [NSView] = [spinner, label]
        var second: NSTextField?
        if let detail {
            let line = NSTextField(wrappingLabelWithString: detail)
            line.preferredMaxLayoutWidth = textWidth
            line.textColor = .secondaryLabelColor
            second = line
            views.append(line)
        }
        if let made {
            made.target = self
            made.action = #selector(pressed(_:))
            if closesOnButton {
                made.keyEquivalent = "\u{1b}"
            }
            views.append(made)
        }
        for view in views {
            view.translatesAutoresizingMaskIntoConstraints = false
            content.addSubview(view)
        }
        var constraints: [NSLayoutConstraint] = [
            content.widthAnchor.constraint(equalToConstant: width),
            spinner.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 20),
            spinner.topAnchor.constraint(equalTo: content.topAnchor, constant: 22),
            label.leadingAnchor.constraint(equalTo: spinner.trailingAnchor, constant: 10),
            label.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -20),
            label.topAnchor.constraint(equalTo: content.topAnchor, constant: 20),
        ]
        var lowest: NSLayoutYAxisAnchor = label.bottomAnchor
        if let second {
            constraints += [
                second.leadingAnchor.constraint(equalTo: label.leadingAnchor),
                second.trailingAnchor.constraint(equalTo: label.trailingAnchor),
                second.topAnchor.constraint(equalTo: label.bottomAnchor, constant: 8),
            ]
            lowest = second.bottomAnchor
        }
        if let made {
            constraints += [
                made.topAnchor.constraint(equalTo: lowest, constant: 16),
                made.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -20),
                made.bottomAnchor.constraint(equalTo: content.bottomAnchor, constant: -20),
            ]
        } else {
            constraints.append(lowest.constraint(equalTo: content.bottomAnchor, constant: -20))
        }
        NSLayoutConstraint.activate(constraints)
        panel.contentView = content
        fit()
    }

    func show() {
        panel.center()
        NSApp.activate()
        panel.makeKeyAndOrderFront(nil)
        panel.orderFrontRegardless()
    }

    func close() {
        panel.orderOut(nil)
    }

    /// Say something else, such as Stopping… once Stop now is chosen.
    func setMessage(_ message: String) {
        label.stringValue = message
        fit()
    }

    func setButtonEnabled(_ enabled: Bool) {
        button?.isEnabled = enabled
    }

    /// Size the window to what it holds, keeping its top left corner where it is.
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

    @objc private func pressed(_ sender: Any?) {
        if closesOnButton {
            close()
        }
        onButton()
    }
}
