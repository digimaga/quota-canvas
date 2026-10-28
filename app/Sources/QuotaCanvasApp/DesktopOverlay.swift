import AppKit
import ImageIO
import SwiftUI

@MainActor
final class DesktopOverlayController {
    static let shared = DesktopOverlayController()
    static let enabledKey = "desktopOverlay.enabled"
    static let positionKey = "desktopOverlay.position"
    static let widthKey = "desktopOverlay.width"
    static let opacityKey = "desktopOverlay.opacity"
    static let aboveIconsKey = "desktopOverlay.aboveIcons"
    static let screenKey = "desktopOverlay.screen"
    static let margin: CGFloat = 16

    enum Position: String, CaseIterable {
        case topLeft, topCenter, topRight, centerLeft, centerRight, bottomLeft, bottomCenter, bottomRight
    }

    struct FrameIdentity: Equatable {
        let modified: Date
        let size: UInt64
    }

    enum FrameLoad {
        case unchanged
        case image(CGImage, FrameIdentity)
        case unavailable
    }

    private let panel: NSPanel
    private let imageView: NSImageView
    private var timer: Timer?
    private var observers: [NSObjectProtocol] = []
    private var started = false
    private var loadGeneration = 0
    private var canDisplay = false
    private var previouslyAvailable = false
    private var frameIdentity: FrameIdentity?

    private init() {
        panel = NSPanel(contentRect: .zero, styleMask: [.borderless, .nonactivatingPanel],
                        backing: .buffered, defer: false)
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = false
        panel.ignoresMouseEvents = true
        panel.collectionBehavior = [.canJoinAllSpaces, .stationary, .ignoresCycle]
        panel.isReleasedWhenClosed = false
        imageView = NSImageView(frame: .zero)
        imageView.imageScaling = .scaleProportionallyUpOrDown
        imageView.wantsLayer = true
        imageView.layer?.cornerRadius = 8
        imageView.layer?.masksToBounds = true
        panel.contentView = imageView
    }

    func start() {
        guard !started else { return }
        started = true
        let center = NotificationCenter.default
        observers.append(center.addObserver(forName: .engineArtifactsChanged, object: nil, queue: .main) { [weak self] _ in
            Task { @MainActor in self?.reload() }
        })
        observers.append(center.addObserver(forName: NSApplication.didChangeScreenParametersNotification, object: nil, queue: .main) { [weak self] _ in
            Task { @MainActor in self?.settingsChanged() }
        })
        let refreshTimer = Timer(timeInterval: 10, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.reload() }
        }
        RunLoop.main.add(refreshTimer, forMode: .common)
        timer = refreshTimer
        settingsChanged()
    }

    func settingsChanged() {
        guard started else { return }
        applySettings()
        let shouldReload = canDisplay && !previouslyAvailable
        previouslyAvailable = canDisplay
        if shouldReload { reload() }
    }

    private func applySettings() {
        guard started else { return }
        let defaults = UserDefaults.standard
        guard defaults.bool(forKey: Self.enabledKey) else {
            canDisplay = false
            loadGeneration += 1
            panel.orderOut(nil)
            return
        }
        let screenID = defaults.string(forKey: Self.screenKey) ?? ""
        guard let screen = NSScreen.screens.first(where: { Self.screenID($0) == screenID }) ?? NSScreen.screens.first else {
            canDisplay = false
            loadGeneration += 1
            panel.orderOut(nil)
            return
        }
        let position = Position(rawValue: defaults.string(forKey: Self.positionKey) ?? "") ?? .topRight
        let width = defaults.object(forKey: Self.widthKey) == nil ? 640 : defaults.double(forKey: Self.widthKey)
        let opacity = defaults.object(forKey: Self.opacityKey) == nil ? 1 : defaults.double(forKey: Self.opacityKey)
        panel.level = NSWindow.Level(rawValue: Int(CGWindowLevelForKey(defaults.bool(forKey: Self.aboveIconsKey) ? .desktopIconWindow : .desktopWindow)) + 1)
        panel.alphaValue = min(1, max(0.3, opacity))
        panel.setFrame(Self.overlayFrame(position: position, width: width, margin: Self.margin, visibleFrame: screen.visibleFrame), display: true)
        canDisplay = true
    }

    static func screenID(_ screen: NSScreen) -> String? {
        (screen.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber)?.stringValue
    }

    static func overlayFrame(position: Position, width: CGFloat, margin: CGFloat, visibleFrame: CGRect) -> CGRect {
        let inset = max(0, margin)
        let actualWidth = max(0, min(width, visibleFrame.width - 2 * inset))
        let height = actualWidth * 462 / 1920
        let x: CGFloat
        let y: CGFloat
        switch position {
        case .topLeft, .centerLeft, .bottomLeft: x = visibleFrame.minX + inset
        case .topCenter, .bottomCenter: x = visibleFrame.midX - actualWidth / 2
        case .topRight, .centerRight, .bottomRight: x = visibleFrame.maxX - inset - actualWidth
        }
        switch position {
        case .topLeft, .topCenter, .topRight: y = visibleFrame.maxY - inset - height
        case .centerLeft, .centerRight: y = visibleFrame.midY - height / 2
        case .bottomLeft, .bottomCenter, .bottomRight: y = visibleFrame.minY + inset
        }
        return CGRect(x: x, y: y, width: actualWidth, height: height)
    }

    private func reload() {
        guard started, canDisplay else { return }
        loadGeneration += 1
        let generation = loadGeneration
        let url = AppPaths.frameURL
        let identity = imageView.image == nil ? nil : frameIdentity
        DispatchQueue.global(qos: .utility).async { [weak self] in
            let result = Self.loadFrame(at: url, previous: identity)
            Task { @MainActor [weak self] in
                guard let self, self.loadGeneration == generation, self.canDisplay else { return }
                switch result {
                case .unavailable:
                    self.imageView.image = nil
                    self.frameIdentity = nil
                    self.panel.orderOut(nil)
                case .unchanged:
                    self.panel.orderFrontRegardless()
                case let .image(image, identity):
                    self.imageView.image = NSImage(cgImage: image, size: .zero)
                    self.frameIdentity = identity
                    self.panel.orderFrontRegardless()
                }
            }
        }
    }

    nonisolated static func loadFrame(at url: URL, previous: FrameIdentity?) -> FrameLoad {
        guard let attributes = try? FileManager.default.attributesOfItem(atPath: url.path),
              let modified = attributes[.modificationDate] as? Date,
              let size = (attributes[.size] as? NSNumber)?.uint64Value else { return .unavailable }
        let identity = FrameIdentity(modified: modified, size: size)
        if identity == previous { return .unchanged }
        let options = [kCGImageSourceShouldCacheImmediately: true] as CFDictionary
        guard let source = CGImageSourceCreateWithURL(url as CFURL, nil),
              let image = CGImageSourceCreateImageAtIndex(source, 0, options) else { return .unavailable }
        return .image(image, identity)
    }
}
