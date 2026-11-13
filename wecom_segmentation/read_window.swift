import AppKit
import ApplicationServices
import Foundation

struct ReadError: LocalizedError {
    let errorDescription: String?
    init(_ message: String) { errorDescription = message }
}

func value(_ element: AXUIElement, _ name: String) -> CFTypeRef? {
    var result: CFTypeRef?
    guard AXUIElementCopyAttributeValue(element, name as CFString, &result) == .success else { return nil }
    return result
}

func children(_ element: AXUIElement, withRole role: String) -> [AXUIElement] {
    (value(element, kAXChildrenAttribute) as? [AXUIElement] ?? []).filter {
        value($0, kAXRoleAttribute) as? String == role
    }
}

func frame(_ element: AXUIElement) -> CGRect? {
    guard let position = value(element, kAXPositionAttribute), let size = value(element, kAXSizeAttribute),
          CFGetTypeID(position) == AXValueGetTypeID(), CFGetTypeID(size) == AXValueGetTypeID() else { return nil }
    var point = CGPoint.zero
    var dimensions = CGSize.zero
    guard AXValueGetValue(position as! AXValue, .cgPoint, &point),
          AXValueGetValue(size as! AXValue, .cgSize, &dimensions) else { return nil }
    return CGRect(origin: point, size: dimensions)
}

func only(_ elements: [AXUIElement]) throws -> AXUIElement {
    guard elements.count == 1, let element = elements.first else {
        throw ReadError("无法唯一定位企微私聊消息区")
    }
    return element
}

struct Bubble: Encodable {
    let speaker: String
    let text: String
}

struct Snapshot: Encodable {
    let header: [String]
    let messages: [Bubble]
}

func isCenteredContactNotice(_ text: String, in region: CGRect, at rect: CGRect) -> Bool {
    // WeCom exposes the centered contact-added notice as a text area, like chat bubbles.
    abs(rect.midX - region.midX) <= 3
        && text.trimmingCharacters(in: .whitespacesAndNewlines).hasPrefix("你已添加")
}

func readWeComWindow(expectedTitle: String) throws -> Snapshot {
    let bundle = "com.tencent.WeWorkMac"
    guard AXIsProcessTrusted() else { throw ReadError("当前采集进程没有 macOS 辅助功能权限") }
    guard let app = NSWorkspace.shared.frontmostApplication, app.bundleIdentifier == bundle else {
        throw ReadError("请将指定企微测试私聊窗口置于前台")
    }
    let root = AXUIElementCreateApplication(app.processIdentifier)
    let windows = value(root, kAXWindowsAttribute) as? [AXUIElement] ?? []
    let window = try only(windows)
    let outer = try only(children(window, withRole: kAXSplitGroupRole))
    let sidebarAndChat = try only(children(outer, withRole: kAXSplitGroupRole))
    let chat = try only(children(sidebarAndChat, withRole: kAXSplitGroupRole))
    let body = try only(children(chat, withRole: kAXSplitGroupRole))
    let areas = children(body, withRole: kAXScrollAreaRole)
    let transcript = try only(areas.filter { children($0, withRole: kAXTableRole).count == 1 })
    let table = try only(children(transcript, withRole: kAXTableRole))
    guard let region = frame(transcript), let chatFrame = frame(chat) else {
        throw ReadError("无法确认企微会话区域的位置")
    }
    func header() -> [String] {
        let labels: [(CGFloat, String)] = children(sidebarAndChat, withRole: kAXStaticTextRole).compactMap { label in
            guard let rect = frame(label), rect.minX >= chatFrame.minX,
                  rect.maxY <= chatFrame.minY,
                  let text = value(label, kAXValueAttribute) as? String, !text.isEmpty else { return nil }
            return (rect.minX, text)
        }
        return labels.sorted { $0.0 < $1.0 }.map { $0.1 }
    }
    let before = header()
    guard before.first == expectedTitle else { throw ReadError("当前企微会话标题与指定标题不符") }
    let bars = children(transcript, withRole: kAXScrollBarRole)
    if let bar = bars.first {
        guard bars.count == 1, let progress = value(bar, kAXValueAttribute) as? NSNumber,
              progress.doubleValue >= 0.995 else { throw ReadError("请将测试私聊滚动到最新消息") }
    }
    var bubbles: [Bubble] = []
    for row in children(table, withRole: kAXRowRole) {
        guard let rowFrame = frame(row), rowFrame.intersects(region) else { continue }
        let cell = try only(children(row, withRole: kAXCellRole))
        let textAreas = children(cell, withRole: kAXTextAreaRole)
        guard textAreas.count <= 1 else { throw ReadError("发现无法识别的消息布局") }
        guard let area = textAreas.first, let rect = frame(area), region.contains(rect),
              !rect.isEmpty, let text = value(area, kAXValueAttribute) as? String,
              !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { continue }
        // On WeCom 5.0.13 text bubbles align to a 26-point inset from either side.
        let isIncoming = abs(rect.minX - region.minX - 26) <= 3
        let isOutgoing = abs(region.maxX - rect.maxX - 26) <= 3
        if isIncoming == isOutgoing {
            if !isIncoming && isCenteredContactNotice(text, in: region, at: rect) { continue }
            throw ReadError("无法判断可见消息的发送方向")
        }
        bubbles.append(Bubble(speaker: isIncoming ? "customer" : "employee", text: text))
    }
    guard before == header(), app.isActive else { throw ReadError("读取时企微会话发生切换，请重试") }
    return Snapshot(header: before, messages: bubbles)
}
