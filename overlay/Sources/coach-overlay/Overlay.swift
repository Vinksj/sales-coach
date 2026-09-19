import AppKit

struct Nudge {
    let id: String
    let callID: String
    let label: String
    let text: String
    let ttl: TimeInterval
}

private func rgb(_ hex: UInt32, _ alpha: CGFloat = 1) -> NSColor {
    NSColor(srgbRed: CGFloat((hex >> 16) & 0xFF) / 255,
            green: CGFloat((hex >> 8) & 0xFF) / 255,
            blue: CGFloat(hex & 0xFF) / 255,
            alpha: alpha)
}

private enum Palette {
    static let card = rgb(0x16120D, 0.92)
    static let text = rgb(0xF5EFE1)
    static let accent = rgb(0xFF5735)
    static let live = rgb(0x5CC98B)
    static let edge = rgb(0xF5EFE1, 0.10)
}

private enum Metrics {
    static let margin: CGFloat = 16
    static let cardWidth: CGFloat = 360
    static let cardMinHeight: CGFloat = 96
    static let padX: CGFloat = 14
    static let padY: CGFloat = 12
    static let gap: CGFloat = 6
    static let pill: CGFloat = 22
    static let dot: CGFloat = 8
    static let radius: CGFloat = 12
}

// A panel that can never become key or main, so it cannot take focus from Zoom.
final class OverlayPanel: NSPanel {
    override var canBecomeKey: Bool { false }
    override var canBecomeMain: Bool { false }
}

final class CardView: NSView {
    var onClick: (() -> Void)?

    private let labelField = NSTextField(labelWithString: "")
    private let textField = NSTextField(wrappingLabelWithString: "")
    private let dot = NSView()
    private(set) var compact = true

    override init(frame: NSRect) {
        super.init(frame: frame)
        wantsLayer = true
        layer?.backgroundColor = Palette.card.cgColor
        layer?.borderColor = Palette.edge.cgColor
        layer?.borderWidth = 1
        layer?.masksToBounds = true

        labelField.font = .monospacedSystemFont(ofSize: 10, weight: .semibold)
        labelField.textColor = Palette.accent
        labelField.lineBreakMode = .byTruncatingTail
        labelField.maximumNumberOfLines = 1

        textField.font = .systemFont(ofSize: 16, weight: .semibold)
        textField.textColor = Palette.text
        textField.maximumNumberOfLines = 3
        textField.lineBreakMode = .byWordWrapping
        textField.cell?.truncatesLastVisibleLine = true
        textField.preferredMaxLayoutWidth = Metrics.cardWidth - 2 * Metrics.padX

        dot.wantsLayer = true
        dot.layer?.backgroundColor = Palette.live.cgColor
        dot.layer?.cornerRadius = Metrics.dot / 2

        for v in [labelField, textField, dot] { addSubview(v) }
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    override var isFlipped: Bool { true }
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }

    // Every click inside the card lands here, not on the labels.
    override func hitTest(_ point: NSPoint) -> NSView? {
        guard let sv = superview else { return nil }
        return bounds.contains(convert(point, from: sv)) ? self : nil
    }

    override func mouseDown(with event: NSEvent) {
        if !compact { onClick?() }
    }

    func showDot() {
        compact = true
        labelField.isHidden = true
        textField.isHidden = true
        layer?.cornerRadius = Metrics.pill / 2
    }

    func show(_ nudge: Nudge) {
        compact = false
        labelField.isHidden = false
        textField.isHidden = false
        labelField.attributedStringValue = NSAttributedString(
            string: nudge.label.uppercased(),
            attributes: [.kern: 1.2, .font: labelField.font as Any, .foregroundColor: Palette.accent])
        textField.stringValue = nudge.text
        layer?.cornerRadius = Metrics.radius
    }

    // Size the view wants for its current mode.
    func fittingCardSize() -> NSSize {
        if compact { return NSSize(width: Metrics.pill, height: Metrics.pill) }
        let textWidth = Metrics.cardWidth - 2 * Metrics.padX
        let labelH = ceil(labelField.intrinsicContentSize.height)
        let textH = ceil(textField.sizeThatFits(NSSize(width: textWidth, height: .greatestFiniteMagnitude)).height)
        let h = Metrics.padY + labelH + Metrics.gap + textH + Metrics.padY
        return NSSize(width: Metrics.cardWidth, height: max(Metrics.cardMinHeight, h))
    }

    override func layout() {
        super.layout()
        if compact {
            dot.frame = NSRect(x: (bounds.width - Metrics.dot) / 2, y: (bounds.height - Metrics.dot) / 2,
                               width: Metrics.dot, height: Metrics.dot)
            return
        }
        let w = bounds.width - 2 * Metrics.padX
        let labelH = ceil(labelField.intrinsicContentSize.height)
        labelField.frame = NSRect(x: Metrics.padX, y: Metrics.padY, width: w - Metrics.dot - 8, height: labelH)
        dot.frame = NSRect(x: bounds.width - Metrics.padX - Metrics.dot,
                           y: Metrics.padY + (labelH - Metrics.dot) / 2,
                           width: Metrics.dot, height: Metrics.dot)
        let textY = Metrics.padY + labelH + Metrics.gap
        textField.frame = NSRect(x: Metrics.padX, y: textY, width: w,
                                 height: bounds.height - textY - Metrics.padY)
    }
}

// Renders idle / listening / nudge from the stream's events.
final class OverlayController {
    var onDismiss: ((Nudge) -> Void)?

    private let panel: OverlayPanel
    private let card: CardView
    private var connected = false
    private var live = false
    private var callID: String?
    private var nudge: Nudge?
    private var generation = 0
    private var expiry: DispatchWorkItem?

    init() {
        panel = OverlayPanel(contentRect: NSRect(x: 0, y: 0, width: Metrics.pill, height: Metrics.pill),
                             styleMask: [.nonactivatingPanel, .borderless],
                             backing: .buffered, defer: true)
        panel.level = .floating
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary]
        panel.isFloatingPanel = true
        panel.hidesOnDeactivate = false
        panel.becomesKeyOnlyIfNeeded = true
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = true
        panel.isMovable = false
        panel.isReleasedWhenClosed = false
        panel.animationBehavior = .none

        card = CardView(frame: panel.contentLayoutRect)
        card.autoresizingMask = [.width, .height]
        panel.contentView = card
        card.onClick = { [weak self] in self?.dismissByClick() }

        NotificationCenter.default.addObserver(
            forName: NSApplication.didChangeScreenParametersNotification, object: nil, queue: .main
        ) { [weak self] _ in self?.render() }
    }

    func setConnected(_ value: Bool) {
        connected = value
        if !value {
            live = false
            callID = nil
            dropNudge()
        }
        render()
    }

    func handle(_ event: [String: Any]) {
        switch event["type"] as? String {
        case "status":
            if (event["state"] as? String) == "listening" {
                let call = Self.string(event["call_id"])
                if let current = nudge, let call, current.callID != call { dropNudge() }
                live = true
                callID = call ?? callID
            } else {
                live = false
                callID = nil
                dropNudge()
            }
            render()
        case "nudge":
            guard let id = Self.string(event["id"]),
                  let text = (event["text"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines),
                  !text.isEmpty
            else { return }
            let call = Self.string(event["call_id"]) ?? callID ?? ""
            let trigger = (event["trigger"] as? String)?.replacingOccurrences(of: "_", with: " ") ?? "coach"
            let label = (event["label"] as? String).flatMap { $0.isEmpty ? nil : $0 } ?? trigger
            let ttl = (event["ttl_s"] as? NSNumber)?.doubleValue ?? 12
            show(Nudge(id: id, callID: call, label: label, text: text, ttl: max(2, ttl)))
        case "clear":
            if let id = Self.string(event["id"]), nudge?.id == id {
                dropNudge()
                render()
            }
        default:
            break
        }
    }

    private func show(_ n: Nudge) {
        // A nudge implies a live call, even if its status event was missed.
        live = true
        if !n.callID.isEmpty { callID = n.callID }
        nudge = n
        generation += 1
        expiry?.cancel()
        let gen = generation
        let work = DispatchWorkItem { [weak self] in self?.expire(gen) }
        expiry = work
        DispatchQueue.main.asyncAfter(deadline: .now() + n.ttl, execute: work)
        render(fadeIn: true)
    }

    private func dropNudge() {
        expiry?.cancel()
        expiry = nil
        nudge = nil
        generation += 1
    }

    private func dismissByClick() {
        guard let n = nudge else { return }
        dropNudge()
        render()
        onDismiss?(n)
    }

    private func expire(_ gen: Int) {
        guard gen == generation, nudge != nil else { return }
        NSAnimationContext.runAnimationGroup({ ctx in
            ctx.duration = 0.35
            panel.animator().alphaValue = 0
        }, completionHandler: { [weak self] in
            guard let self, gen == self.generation else { return }
            self.dropNudge()
            self.render(fadeIn: true)
        })
    }

    private func render(fadeIn: Bool = false) {
        guard connected, live else {
            panel.orderOut(nil)
            return
        }
        if let n = nudge {
            card.show(n)
            panel.ignoresMouseEvents = false
        } else {
            card.showDot()
            // The dot is decoration only; let clicks fall through to Zoom.
            panel.ignoresMouseEvents = true
        }
        let size = card.fittingCardSize()
        let screen = NSScreen.main ?? NSScreen.screens.first
        let vf = screen?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1440, height: 900)
        let frame = NSRect(x: vf.maxX - Metrics.margin - size.width,
                           y: vf.maxY - Metrics.margin - size.height,
                           width: size.width, height: size.height)
        panel.setFrame(frame, display: true)
        card.needsLayout = true
        panel.invalidateShadow()

        if fadeIn {
            panel.alphaValue = 0
            panel.orderFrontRegardless()
            NSAnimationContext.runAnimationGroup { ctx in
                ctx.duration = 0.2
                panel.animator().alphaValue = 1
            }
        } else {
            panel.alphaValue = 1
            panel.orderFrontRegardless()
        }
    }

    private static func string(_ value: Any?) -> String? {
        switch value {
        case let s as String: return s.isEmpty ? nil : s
        case let n as NSNumber: return n.stringValue
        default: return nil
        }
    }
}
