import AppKit
import ApplicationServices

let appURL = URL(fileURLWithPath: CommandLine.arguments[1]).resolvingSymlinksInPath()
let folder = URL(fileURLWithPath: CommandLine.arguments[2])
let launch = Process()
launch.executableURL = URL(fileURLWithPath: "/usr/bin/open")
launch.arguments = ["-n", appURL.path]
try launch.run()
launch.waitUntilExit()
var instance: NSRunningApplication?
for _ in 0..<50 {
    instance = NSWorkspace.shared.runningApplications.first {
        $0.bundleURL?.resolvingSymlinksInPath().path == appURL.path
    }
    if instance != nil { break }
    Thread.sleep(forTimeInterval: 0.2)
}
guard let result = instance else { fatalError("未找到测试应用") }
let pid = result.processIdentifier
let app = AXUIElementCreateApplication(pid)
defer { result.terminate() }
let deadline = Date().addingTimeInterval(20)

func attr(_ element: AXUIElement, _ name: String) -> AnyObject? {
    var value: CFTypeRef?
    guard AXUIElementCopyAttributeValue(element, name as CFString, &value) == .success else { return nil }
    return value
}

func children(_ element: AXUIElement) -> [AXUIElement] {
    let direct = attr(element, kAXChildrenAttribute) as? [AXUIElement] ?? []
    return [element] + direct.flatMap(children)
}

func windowInfo() -> (Int, CGRect)? {
    let windows = CGWindowListCopyWindowInfo(.optionOnScreenOnly, kCGNullWindowID) as? [[String: Any]] ?? []
    for item in windows where item[kCGWindowOwnerPID as String] as? Int32 == pid {
        guard item[kCGWindowLayer as String] as? Int == 0,
              let number = item[kCGWindowNumber as String] as? Int,
              let value = item[kCGWindowBounds as String] as? NSDictionary,
              let bounds = CGRect(dictionaryRepresentation: value as CFDictionary),
              bounds.width >= 800 else { continue }
        return (number, bounds)
    }
    return nil
}

func screenshot(_ filename: String) throws {
    guard let (number, _) = windowInfo() else { fatalError("工作台窗口未显示") }
    let capture = Process()
    capture.executableURL = URL(fileURLWithPath: "/usr/sbin/screencapture")
    capture.arguments = ["-x", "-o", "-l", String(number), folder.appendingPathComponent(filename).path]
    try capture.run()
    capture.waitUntilExit()
    guard capture.terminationStatus == 0 else { fatalError("截图失败") }
}


func find(_ label: String, requiringAction action: String? = nil) -> AXUIElement? {
    let windows = attr(app, kAXWindowsAttribute) as? [AXUIElement] ?? []
    return windows.flatMap(children).first { element in
        if let action {
            var actions: CFArray?
            guard AXUIElementCopyActionNames(element, &actions) == .success,
                  (actions as? [String])?.contains(action) == true else { return false }
        }
        return [kAXTitleAttribute, kAXDescriptionAttribute, kAXIdentifierAttribute, kAXValueAttribute].contains {
            (attr(element, $0) as? String) == label
        }
    }
}

func waitFor(_ label: String, requiringAction action: String? = nil) throws -> AXUIElement {
    let until = Date().addingTimeInterval(30)
    while Date() < until {
        if let element = find(label, requiringAction: action) { return element }
        Thread.sleep(forTimeInterval: 0.15)
    }
    if windowInfo() != nil { try screenshot("debug.png") }
    for window in attr(app, kAXWindowsAttribute) as? [AXUIElement] ?? [] {
        for element in children(window) {
            let labels = [kAXTitleAttribute, kAXDescriptionAttribute, kAXIdentifierAttribute, kAXValueAttribute]
                .compactMap { attr(element, $0) as? String }.joined(separator: " ")
            if !labels.isEmpty { FileHandle.standardError.write(Data((labels + "\n").utf8)) }
        }
    }
    throw NSError(domain: "Workbench AX", code: 1, userInfo: [NSLocalizedDescriptionKey: "缺少 " + label])
}

func press(_ label: String) throws {
    let button = try waitFor(label, requiringAction: kAXPressAction)
    guard AXUIElementPerformAction(button, kAXPressAction as CFString) == .success else {
        throw NSError(domain: "Workbench AX", code: 2, userInfo: [NSLocalizedDescriptionKey: "无法点击 " + label])
    }
}

func fill(_ label: String, _ text: String) throws {
    let field = try waitFor(label)

    var point = CGPoint.zero
    var size = CGSize.zero
    guard let rawPoint = attr(field, kAXPositionAttribute), let rawSize = attr(field, kAXSizeAttribute),
          AXValueGetValue(rawPoint as! AXValue, .cgPoint, &point),
          AXValueGetValue(rawSize as! AXValue, .cgSize, &size) else { fatalError("无法定位 " + label) }
    point.x += size.width / 2
    point.y += size.height / 2
    CGEvent(mouseEventSource: nil, mouseType: .leftMouseDown, mouseCursorPosition: point, mouseButton: .left)?.post(tap: .cghidEventTap)
    CGEvent(mouseEventSource: nil, mouseType: .leftMouseUp, mouseCursorPosition: point, mouseButton: .left)?.post(tap: .cghidEventTap)
    Thread.sleep(forTimeInterval: 0.2)
    let codes = Array(text.utf16)
    for code in codes {
        for down in [true, false] {
            let event = CGEvent(keyboardEventSource: nil, virtualKey: 0, keyDown: down)
            event?.keyboardSetUnicodeString(stringLength: 1, unicodeString: [code])
            event?.post(tap: .cghidEventTap)
        }
    }
    Thread.sleep(forTimeInterval: 0.4)
}

guard AXIsProcessTrusted() else {
    fatalError("请在辅助功能中授权运行测试的终端")
}
while windowInfo() == nil && Date() < deadline { Thread.sleep(forTimeInterval: 0.2) }

if CommandLine.arguments.contains("--connection-timeout") {
    _ = try waitFor("无法连接本地工作区")
    _ = try waitFor("本地服务在 15 秒内未响应。项目放在桌面时，请在「系统设置 → 隐私与安全性 → 文件与文件夹」允许工作台访问桌面文件夹，再点「重新连接」。")
    _ = try waitFor("重新连接", requiringAction: kAXPressAction)
    try screenshot("09-本地连接超时.png")
    result.terminate()
    exit(EXIT_SUCCESS)
}

func checkStudyProfile() throws {
    try press("人群标签演示, 同事测试")
    _ = try waitFor("人群标签")
    _ = try waitFor("初中生")
    _ = try waitFor("家长")
    _ = try waitFor("推进中")
    _ = try waitFor("用户基本情况")
    _ = try waitFor("函数题容易丢分")
    try screenshot("05-用户基本情况与人群标签.png")
}

if CommandLine.arguments.contains("--study-profile") {
    try checkStudyProfile()
    result.terminate()
    exit(EXIT_SUCCESS)
}

_ = try waitFor("采集并分层")
_ = try waitFor("session-api-settings")
precondition(find("重试本地分层") == nil, "未采集的会话不应显示重试")
try screenshot("01-首次采集和飞书入口.png")
try press("查看飞书表格")
_ = try waitFor("飞书表格尚未配置")
Thread.sleep(forTimeInterval: 0.4)
try screenshot("02-飞书配置提示.png")
try press("好")
try press("session-api-settings")
_ = try waitFor("模型 API 配置")
Thread.sleep(forTimeInterval: 0.4)
try screenshot("03-模型API配置.png")
try fill("api-base-url", "https://model.example/v1")
try fill("api-model", "example-chat")
try fill("api-key", "fixture-key")
let save = try waitFor("保存")
precondition((attr(save, kAXEnabledAttribute) as? Bool) == true, "API 表单应可保存")
try press("保存")
_ = try waitFor("模型 API")
try press("待分层会话, 同事测试")
let retry = try waitFor("重试本地分层")
precondition((attr(retry, kAXEnabledAttribute) as? Bool) == true, "采集后待分层的会话应可重试")
Thread.sleep(forTimeInterval: 0.4)
try screenshot("04-待分层可重试.png")
try checkStudyProfile()
try press("未采集会话, 同事测试")
try press("添加会话")
try fill("session-title", "人群标签演示")
_ = try waitFor("打开已有会话", requiringAction: kAXPressAction)
try screenshot("06-打开已添加的会话.png")
try press("打开已有会话")
_ = try waitFor("初中生")
let sessions = (attr(app, kAXWindowsAttribute) as? [AXUIElement] ?? []).flatMap(children)
    .filter { (attr($0, kAXDescriptionAttribute) as? String) == "人群标签演示, 同事测试" }
precondition(sessions.count == 1, "再次添加同名会话后应只有一条")
try screenshot("07-已有会话和分层结果.png")
try press("添加会话")
_ = try waitFor("企微获客")
try fill("session-title", "默认获客会话")
try screenshot("08-企微获客默认选项.png")
try press("添加")
_ = try waitFor("默认获客会话, 企微获客")
print("原生界面验证通过")
