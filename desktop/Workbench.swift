import AppKit
import ApplicationServices
import SwiftUI

private let brand = Color(red: 0.06, green: 0.43, blue: 0.32)
private let acquisitionScope = "企微获客"

struct Destination: Codable, Equatable {
    let url: String
    let model: String
}

struct Preview: Decodable {
    private let fields: [String: String]

    init(from decoder: Decoder) throws {
        let values = try decoder.singleValueContainer().decode([String: FieldValue].self)
        fields = values.mapValues(\.text)
    }

    subscript(_ key: String) -> String {
        guard let value = fields[key], !value.isEmpty else { return "—" }
        return value
    }

    private enum FieldValue: Decodable {
        case string(String), number(Int), list([String])

        init(from decoder: Decoder) throws {
            let value = try decoder.singleValueContainer()
            if let text = try? value.decode(String.self) { self = .string(text) }
            else if let number = try? value.decode(Int.self) { self = .number(number) }
            else { self = .list(try value.decode([String].self)) }
        }

        var text: String {
            switch self {
            case .string(let value): return value
            case .list(let values): return values.joined(separator: "、")
            case .number(let value):
                if value < 1_000_000_000 { return String(value) }
                return Date(timeIntervalSince1970: Double(value) / 1000)
                    .formatted(.dateTime.year().month().day().hour().minute())
            }
        }
    }
}

struct Conversation: Decodable, Identifiable {
    let id: String
    let title: String
    let customer: Bool
    let status: String
    let sync: String
    let preview: Preview?

    var scope: String { customer ? acquisitionScope : "同事测试" }
}

struct Job: Decodable {
    let id: String
    let session: String
    let status: String
    let message: String
    let captured: Int?
    let synced: [String: SyncCount]?
    var running: Bool { status == "running" }
    var failed: Bool { status == "error" }
}

enum SyncCount: Decodable {
    case count(Int), reasons

    init(from decoder: Decoder) throws {
        let value = try decoder.singleValueContainer()
        if let count = try? value.decode(Int.self) { self = .count(count) }
        else { self = .reasons }
    }
}

struct Workspace: Decodable {
    let sessions: [Conversation]
    let feishu: Bool
    let feishu_url: URL?
    let api: Destination?
    let job: Job?
}

private struct Command: Encodable {
    let id = UUID().uuidString
    let method: String
    var title: String? = nil
    var customer: Bool? = nil
    var session: String? = nil
    var action: String? = nil
    var destination: Destination? = nil
    var url: String? = nil
    var model: String? = nil
    var key: String? = nil
    var snapshot: Snapshot? = nil
}

private struct Reply<Value: Decodable>: Decodable {
    let data: Value?
    let error: String?
}

private struct WorkbenchError: LocalizedError {
    let message: String
    var errorDescription: String? { message }
}

@MainActor
final class Worker {
    private let process = Process()
    private let input = Pipe()
    private let output = Pipe()
    private var buffer = Data()
    private var replies: [String: (Result<Data, Error>) -> Void] = [:]
    private var timeouts: [String: Task<Void, Never>] = [:]

    init() throws {
        let project = Bundle.main.bundleURL.deletingLastPathComponent().deletingLastPathComponent()
        let local = project.appendingPathComponent(".local", isDirectory: true)
        let python = local.appendingPathComponent("venv/bin/python")
        guard FileManager.default.isExecutableFile(atPath: python.path) else {
            throw WorkbenchError(message: "请从项目中的「打开企微分层工作台.command」启动，并检查 Python 环境。")
        }
        let log = local.appendingPathComponent("workbench.log")
        if !FileManager.default.fileExists(atPath: log.path) {
            FileManager.default.createFile(atPath: log.path, contents: nil, attributes: [.posixPermissions: 0o600])
        }
        let logHandle = try FileHandle(forWritingTo: log)
        try logHandle.seekToEnd()
        process.executableURL = python
        process.currentDirectoryURL = project
        process.arguments = ["-m", "wecom_segmentation.desktop"]
        var environment = ProcessInfo.processInfo.environment
        environment["REQUESTS_CA_BUNDLE"] = environment["REQUESTS_CA_BUNDLE"] ?? "/etc/ssl/cert.pem"
        process.environment = environment
        process.standardInput = input
        process.standardOutput = output
        process.standardError = logHandle
        try process.run()
        try logHandle.close()
        readOutput()
    }

    private func readOutput() {
        let pipe = output.fileHandleForReading
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            while true {
                let data = pipe.availableData
                if data.isEmpty { break }
                DispatchQueue.main.async { [weak self] in self?.receive(data) }
            }
            DispatchQueue.main.async { [weak self] in
                guard let self else { return }
                let error = WorkbenchError(message: "本地服务已退出。请重新打开工作台，或查看 .local/workbench.log。")
                self.failPending(error)
            }
        }
    }

    private func receive(_ data: Data) {
        buffer.append(data)
        while let newline = buffer.firstIndex(of: 10) {
            let line = Data(buffer[..<newline])
            buffer.removeSubrange(...newline)
            guard let envelope = try? JSONDecoder().decode(ReplyID.self, from: line) else { continue }
            finish(envelope.id, with: .success(line))
        }
    }

    private struct ReplyID: Decodable { let id: String }

    private func finish(_ id: String, with result: Result<Data, Error>) {
        timeouts.removeValue(forKey: id)?.cancel()
        replies.removeValue(forKey: id)?(result)
    }

    private func failPending(_ error: Error) {
        for id in Array(replies.keys) {
            finish(id, with: .failure(error))
        }
    }

    fileprivate func request<Value: Decodable>(_ command: Command, as type: Value.Type) async throws -> Value {
        guard process.isRunning else { throw WorkbenchError(message: "本地服务已停止，请重新打开工作台。") }
        var data = try JSONEncoder().encode(command)
        data.append(10)
        return try await withCheckedThrowingContinuation { continuation in
            replies[command.id] = { result in
                continuation.resume(with: result.flatMap { raw in
                    Result {
                        let response = try JSONDecoder().decode(Reply<Value>.self, from: raw)
                        guard let value = response.data else {
                            throw WorkbenchError(message: response.error ?? "无法读取操作结果")
                        }
                        return value
                    }
                })
            }
            timeouts[command.id] = Task { [weak self] in
                do { try await Task.sleep(for: .seconds(15)) }
                catch { return }
                guard let self else { return }
                let error = WorkbenchError(message: "本地服务在 15 秒内未响应。项目放在桌面时，请在「系统设置 → 隐私与安全性 → 文件与文件夹」允许工作台访问桌面文件夹，再点「重新连接」。")
                self.failPending(error)
                if self.process.isRunning { self.process.terminate() }
            }
            do { try input.fileHandleForWriting.write(contentsOf: data) }
            catch { finish(command.id, with: .failure(error)) }
        }
    }

    func stop() {
        try? input.fileHandleForWriting.close()
    }
}

@MainActor
final class WorkbenchModel: ObservableObject {
    @Published var workspace: Workspace?
    @Published var selection: String?
    @Published var filter = ""
    @Published var error: String?
    @Published var submitting = false
    @Published private(set) var connectionIssue: String?
    private var worker: Worker?
    private var refreshing = false

    var selected: Conversation? { workspace?.sessions.first { $0.id == selection } }
    var busy: Bool { submitting || workspace?.job?.running == true }
    var filtered: [Conversation] {
        (workspace?.sessions ?? []).filter {
            filter.isEmpty || $0.title.localizedCaseInsensitiveContains(filter)
        }
    }

    func refresh() async {
        guard !refreshing else { return }
        refreshing = true
        defer { refreshing = false }
        do {
            if workspace == nil { connectionIssue = nil }
            if worker == nil { worker = try Worker() }
            guard let worker else { return }
            workspace = try await worker.request(Command(method: "state"), as: Workspace.self)
            connectionIssue = nil
            if selected == nil { selection = workspace?.sessions.first?.id }
        } catch {
            connectionIssue = error.localizedDescription
            if workspace != nil { self.error = error.localizedDescription }
            worker?.stop()
            worker = nil
        }
    }

    func add(_ title: String, customer: Bool) async -> Bool {
        guard let worker else { return false }
        submitting = true
        defer { submitting = false }
        do {
            let result = try await worker.request(Command(method: "add", title: title, customer: customer),
                                                  as: [String: String].self)
            filter = ""
            await refresh()
            selection = result["id"]
            return true
        } catch {
            self.error = error.localizedDescription
            return false
        }
    }

    func saveAPI(url: String, model: String, key: String) async throws {
        guard let worker else { throw WorkbenchError(message: "请先连接本地工作区。") }
        submitting = true
        defer { submitting = false }
        _ = try await worker.request(Command(method: "save_api", url: url, model: model, key: key),
                                     as: Destination.self)
        await refresh()
    }

    func start(_ action: String, session: String, destination: Destination? = nil) async {
        guard let worker, !busy else { return }
        submitting = true
        defer { submitting = false }
        do {
            var snapshot: Snapshot?
            if ["capture", "api_capture"].contains(action) {
                guard let conversation = workspace?.sessions.first(where: { $0.id == session }) else { return }
                let options = ["AXTrustedCheckOptionPrompt": true] as CFDictionary
                guard AXIsProcessTrustedWithOptions(options) else {
                    throw WorkbenchError(message: "macOS 尚未授权当前运行的工作台。若「企微分层工作台」已经打开开关，请在辅助功能列表移除旧条目，重新添加当前项目的 .local/企微分层工作台.app，再退出并重新打开工作台。")
                }
                let open = Process()
                open.executableURL = URL(fileURLWithPath: "/usr/bin/open")
                open.arguments = ["-b", "com.tencent.WeWorkMac"]
                try open.run()
                open.waitUntilExit()
                guard open.terminationStatus == 0 else {
                    throw WorkbenchError(message: "无法打开企业微信，请启动 Mac 版企微后重试。")
                }
                try await Task.sleep(for: .seconds(1))
                snapshot = try readWeComWindow(expectedTitle: conversation.title)
            }
            _ = try await worker.request(Command(method: "start", session: session, action: action,
                                                  destination: destination, snapshot: snapshot), as: Job.self)
            await refresh()
        } catch {
            self.error = error.localizedDescription
        }
    }

    func monitor() async {
        await refresh()
        var nextIdleRefresh = Date().addingTimeInterval(60)
        while !Task.isCancelled {
            do { try await Task.sleep(for: .milliseconds(750)) }
            catch { break }
            if !submitting && (workspace?.job?.running == true || Date() >= nextIdleRefresh) {
                await refresh()
                nextIdleRefresh = Date().addingTimeInterval(60)
            }
        }
    }

    func quitAllowed() -> Bool {
        guard !busy else {
            let alert = NSAlert()
            alert.messageText = "当前操作正在进行"
            alert.informativeText = "请等待操作完成后再退出工作台。"
            alert.runModal()
            return false
        }
        worker?.stop()
        return true
    }
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    let model = WorkbenchModel()

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.activate(ignoringOtherApps: true)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        model.quitAllowed() ? .terminateNow : .terminateCancel
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if !flag { sender.windows.first { $0.canBecomeMain }?.makeKeyAndOrderFront(nil) }
        return true
    }
}

@main
struct WorkbenchApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate

    var body: some Scene {
        Window("企微分层工作台", id: "workbench") {
            WorkbenchView(model: delegate.model)
                .frame(minWidth: 860, minHeight: 620)
                .tint(brand)
        }
        .defaultSize(width: 1100, height: 760)
    }
}

struct WorkbenchView: View {
    @ObservedObject var model: WorkbenchModel
    @Environment(\.openURL) private var openURL
    @State private var adding = false
    @State private var editingAPI = false
    @State private var showingFeishuSetup = false
    @State private var confirmation: Confirmation?

    var body: some View {
        HStack(spacing: 0) {
            sidebar.frame(width: 254)
            Divider()
            ScrollView {
                VStack(alignment: .leading, spacing: 24) {
                    header
                    if let session = model.selected {
                        operations(session)
                        if let job = model.workspace?.job, job.session == session.id { jobBanner(job) }
                        results(session)
                    } else {
                        empty
                    }
                }
                .padding(34)
                .frame(maxWidth: 980)
                .frame(maxWidth: .infinity, alignment: .top)
            }
            .background(Color(nsColor: .windowBackgroundColor))
        }
        .task { await model.monitor() }
        .sheet(isPresented: $adding) { AddSheet(model: model, isPresented: $adding) }
        .sheet(isPresented: $editingAPI) { APISettingsSheet(model: model) }
        .alert("飞书表格尚未配置", isPresented: $showingFeishuSetup) {
            Button("好", role: .cancel) {}
        } message: {
            Text("请按 README 中的「接入飞书测试表」填写 .local/feishu.env，再点击左侧底部的刷新按钮。")
        }
        .sheet(item: $confirmation) { item in
            VStack(alignment: .leading, spacing: 16) {
                Label(item.title, systemImage: item.icon).font(.title2.bold())
                Text(item.session.title).font(.headline)
                Text(item.explanation).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                if let api = item.destination {
                    VStack(alignment: .leading, spacing: 6) {
                        Text("发送目的地").font(.caption).foregroundStyle(.secondary)
                        Text(api.url).textSelection(.enabled)
                        Text(api.model).font(.callout).textSelection(.enabled)
                    }.padding(12).frame(maxWidth: .infinity, alignment: .leading).background(.quaternary, in: RoundedRectangle(cornerRadius: 8))
                }
                HStack {
                    Spacer()
                    Button("取消") { confirmation = nil }.keyboardShortcut(.cancelAction)
                    Button(item.button) {
                        confirmation = nil
                        Task { await model.start(item.action, session: item.session.id, destination: item.destination) }
                    }.buttonStyle(.borderedProminent).keyboardShortcut(.defaultAction)
                }
            }.padding(24).frame(width: 465)
        }
        .alert("操作未完成", isPresented: Binding(get: { model.error != nil }, set: { if !$0 { model.error = nil } })) {
            Button("好") { model.error = nil }
        } message: {
            Text(model.error ?? "")
        }
    }

    private var sidebar: some View {
        VStack(alignment: .leading, spacing: 17) {
            HStack(spacing: 10) {
                Image(systemName: "square.stack.3d.up.fill")
                    .font(.system(size: 17, weight: .semibold))
                    .foregroundStyle(.white)
                    .frame(width: 38, height: 38)
                    .background(brand, in: RoundedRectangle(cornerRadius: 10))
                VStack(alignment: .leading, spacing: 3) {
                    Text("企微分层").font(.system(size: 16, weight: .bold))
                    Text("运营工作台").font(.caption).foregroundStyle(.secondary)
                }
            }.padding(.horizontal, 4)
            HStack {
                Text("会话").font(.subheadline.bold())
                Spacer()
                Button { adding = true } label: { Image(systemName: "plus") }
                    .help("添加会话").accessibilityLabel("添加会话").disabled(model.workspace == nil)
            }
            HStack(spacing: 6) {
                Image(systemName: "magnifyingglass").foregroundStyle(.secondary)
                TextField("搜索会话", text: $model.filter).textFieldStyle(.plain)
            }.padding(9).background(.background, in: RoundedRectangle(cornerRadius: 8))
            ScrollView {
                VStack(alignment: .leading, spacing: 5) {
                    ForEach(model.filtered) { session in
                        Button {
                            model.selection = session.id
                        } label: {
                            HStack(spacing: 9) {
                                Image(systemName: session.customer ? "person.crop.circle" : "bubble.left")
                                    .foregroundStyle(brand)
                                    .frame(width: 27, height: 27)
                                    .background(brand.opacity(0.10), in: RoundedRectangle(cornerRadius: 7))
                                VStack(alignment: .leading, spacing: 3) {
                                    Text(session.title).font(.subheadline.weight(.semibold)).lineLimit(1)
                                    Text(session.scope).font(.caption).foregroundStyle(.secondary)
                                }
                                Spacer(minLength: 0)
                                if model.workspace?.job?.session == session.id && model.busy {
                                    ProgressView().controlSize(.mini)
                                } else if model.selection == session.id {
                                    Image(systemName: "chevron.right").font(.caption).foregroundStyle(brand)
                                }
                            }.padding(8).contentShape(Rectangle())
                        }.buttonStyle(.plain)
                            .background(model.selection == session.id ? brand.opacity(0.13) : .clear,
                                        in: RoundedRectangle(cornerRadius: 9))
                    }
                    if !model.filter.isEmpty && model.filtered.isEmpty {
                        Text("没有匹配的会话").font(.caption).foregroundStyle(.secondary).padding(8)
                    }
                }.frame(maxWidth: .infinity, alignment: .leading)
            }
            Divider()
            Button { editingAPI = true } label: {
                Label(model.workspace?.api == nil ? "添加模型 API" : "配置模型 API",
                      systemImage: "key")
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            .accessibilityIdentifier("sidebar-api-settings")
            .disabled(model.workspace == nil || model.busy)
            HStack {
                Label("本机工作区", systemImage: "internaldrive").font(.caption).foregroundStyle(.secondary)
                Spacer()
                Button { Task { await model.refresh() } } label: { Image(systemName: "arrow.clockwise") }
                    .help("刷新本地状态和配置").accessibilityLabel("刷新").disabled(model.submitting)
            }
        }
        .padding(16)
        .background(.regularMaterial)
    }

    private var header: some View {
        HStack(alignment: .top) {
            VStack(alignment: .leading, spacing: 7) {
                Text(model.selected?.title ?? "会话工作台").font(.largeTitle.bold()).textSelection(.enabled)
                Text(model.selected?.scope ?? "管理企微私聊的采集、分层和同步")
                    .font(.subheadline).foregroundStyle(.secondary)
            }
            Spacer()
            VStack(alignment: .trailing, spacing: 9) {
                Button {
                    if let url = model.workspace?.feishu_url {
                        openURL(url)
                    } else {
                        showingFeishuSetup = true
                    }
                } label: {
                    Label("查看飞书表格", systemImage: "tablecells")
                }.help("在默认浏览器打开已配置的飞书表格")
                    .disabled(model.workspace == nil)
                if let session = model.selected {
                    HStack {
                        badge(session.status, positive: session.status == "已分层")
                        badge(model.workspace?.feishu == true ? session.sync : "飞书未配置",
                              positive: session.sync == "已同步")
                    }
                }
            }
        }
    }

    private var empty: some View {
        VStack(spacing: 13) {
            if model.workspace == nil {
                if let issue = model.connectionIssue {
                    Label("无法连接本地工作区", systemImage: "exclamationmark.circle").font(.headline)
                    Text(issue).foregroundStyle(.secondary)
                    Button("重新连接") { Task { await model.refresh() } }
                } else {
                    ProgressView().controlSize(.small)
                    Text("正在连接本地工作区").foregroundStyle(.secondary)
                }
            } else {
                Image(systemName: "bubble.left.and.bubble.right")
                    .font(.system(size: 28, weight: .medium))
                    .foregroundStyle(brand).frame(width: 66, height: 66)
                    .background(brand.opacity(0.12), in: RoundedRectangle(cornerRadius: 18))
                Text("添加第一条企微会话").font(.title2.bold())
                Text("使用企微聊天顶部显示的完整会话名。").foregroundStyle(.secondary)
                Button { adding = true } label: { Label("添加会话", systemImage: "plus") }
                    .buttonStyle(.borderedProminent)
            }
        }.frame(maxWidth: .infinity).padding(.vertical, 62).card()
    }

    private func operations(_ session: Conversation) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            Label("会话操作", systemImage: "slider.horizontal.3").font(.headline)
            HStack(spacing: 10) {
                Button {
                    confirmation = Confirmation(action: "capture", session: session)
                } label: { Label("采集并分层", systemImage: "text.viewfinder") }
                    .buttonStyle(.borderedProminent)
                if session.preview != nil {
                    Button {
                        confirmation = Confirmation(action: "local_reprocess", session: session)
                    } label: { Label("重新本地分层", systemImage: "arrow.clockwise") }
                } else if ["待分层", "分层失败"].contains(session.status) {
                    Button {
                        Task { await model.start("classify", session: session.id) }
                    } label: { Label("重试本地分层", systemImage: "arrow.clockwise") }
                }
                if let api = model.workspace?.api {
                    Menu {
                        Button("采集并用 API 分层") {
                            confirmation = Confirmation(action: "api_capture", session: session, destination: api)
                        }
                        if session.status != "未采集" {
                            Button("使用已采集消息重新分层") {
                                confirmation = Confirmation(action: "api_reprocess", session: session, destination: api)
                            }
                        }
                    } label: {
                        Label("模型 API", systemImage: "sparkles")
                    }.fixedSize()
                } else {
                    Button { editingAPI = true } label: {
                        Label("配置模型 API", systemImage: "sparkles")
                    }.accessibilityIdentifier("session-api-settings")
                }
                Spacer(minLength: 2)
                Button {
                    confirmation = Confirmation(action: "sync", session: session)
                } label: { Label("同步飞书", systemImage: "arrow.up.right.square") }
                    .disabled(session.preview == nil || model.workspace?.feishu != true)
            }.controlSize(.regular).disabled(model.busy)
            HStack(spacing: 12) {
                Label("默认使用本机 Ollama", systemImage: "cpu")
                if session.status == "未采集" { Text("首次使用请先采集聊天记录") }
                if model.workspace?.feishu != true { Text("请配置飞书测试表后刷新") }
            }.font(.caption).foregroundStyle(.secondary)
            if let job = model.workspace?.job, job.running && job.session != session.id {
                Text("另一条会话正在处理").font(.callout).foregroundStyle(.secondary)
            }
        }.padding(18).card()
    }

    private func jobBanner(_ job: Job) -> some View {
        HStack(alignment: .top, spacing: 10) {
            if job.running { ProgressView().controlSize(.small) }
            else {
                Image(systemName: job.failed ? "exclamationmark.circle" : "checkmark.circle")
                    .foregroundStyle(job.failed ? Color.orange : brand)
            }
            VStack(alignment: .leading, spacing: 4) {
                Text(job.message).font(.subheadline.weight(.semibold))
                if let captured = job.captured {
                    Text("本次新增 \(captured) 条文字消息").font(.caption).foregroundStyle(.secondary)
                }
                if let synced = job.synced {
                    let summary = ["新增", "更新", "已是最新", "恢复", "失败", "远端较新"].compactMap { label in
                        if case .count(let count) = synced[label], count > 0 { return "\(label) \(count)" }
                        return nil
                    }.joined(separator: " · ")
                    if !summary.isEmpty { Text(summary).font(.caption).foregroundStyle(.secondary) }
                }
            }
            Spacer()
        }.padding(13).frame(maxWidth: .infinity, alignment: .leading)
            .background((job.failed ? Color.orange : brand).opacity(0.10), in: RoundedRectangle(cornerRadius: 10))
            .accessibilityElement(children: .combine)
    }

    private func results(_ session: Conversation) -> some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack {
                Label("分层预览", systemImage: "rectangle.grid.2x2").font(.headline)
                Spacer()
                if let preview = session.preview {
                    Text("版本 \(preview["同步版本"])").font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                }
            }
            if let preview = session.preview {
                if preview["人群标签"] != "—" {
                    HStack(spacing: 8) {
                        Text("人群标签").font(.caption).foregroundStyle(.secondary)
                        ForEach(preview["人群标签"].components(separatedBy: "、"), id: \.self) { tag in
                            let color = tag == "初中生" ? Color.orange : brand
                            Label(tag, systemImage: tag == "初中生" ? "graduationcap" : "person")
                                .font(.callout.weight(.semibold))
                                .foregroundStyle(color)
                                .padding(.horizontal, 10).padding(.vertical, 5)
                                .background(color.opacity(0.12), in: Capsule())
                        }
                    }
                }
                HStack(spacing: 10) {
                    metric("当前阶段", preview, "point.topleft.down.to.point.bottomright.curvepath")
                    metric("有效线索判定", preview, "checkmark.seal")
                    metric("来源", preview, "arrow.down.to.line")
                }
                Divider()
                LazyVGrid(columns: [GridItem(.adaptive(minimum: 148), alignment: .leading)], alignment: .leading, spacing: 17) {
                    ForEach(["咨询人身份", "微信ID", "年级", "选科组合", "学习目标", "主要卡点", "跟进状态",
                             "当前有效试听场次", "到课待核实", "付费待核实"], id: \.self) { key in
                        field(key, key == "选科组合" && preview["科目"] == "—" ? "待确认" :
                              preview[key == "选科组合" ? "科目" : key])
                    }
                }
                Divider()
                field("用户基本情况", preview["用户基本情况"])
                Divider()
                field("证据摘要", preview["证据摘要"])
            } else {
                VStack(spacing: 8) {
                    Image(systemName: "doc.text.magnifyingglass").font(.title2).foregroundStyle(.secondary)
                    Text("暂无可预览的分层结果").font(.subheadline.bold())
                    Text(session.status == "未采集" ? "完成采集和分层后，可在这里查看结果。" : "分层完成后，可在这里查看最新结果。")
                        .font(.callout).foregroundStyle(.secondary)
                }.frame(maxWidth: .infinity).padding(.vertical, 34)
            }
        }.padding(18).card()
    }

    private func metric(_ name: String, _ preview: Preview, _ icon: String) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Label(name, systemImage: icon).font(.caption).foregroundStyle(.secondary)
            Text(preview[name]).font(.system(size: 17, weight: .semibold)).textSelection(.enabled)
        }.frame(maxWidth: .infinity, alignment: .leading).padding(12)
            .background(brand.opacity(0.08), in: RoundedRectangle(cornerRadius: 9))
    }

    private func field(_ key: String, _ value: String) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            Text(key).font(.caption).foregroundStyle(.secondary)
            Text(value).font(.callout.weight(.medium)).textSelection(.enabled)
        }
    }

    private func badge(_ text: String, positive: Bool) -> some View {
        Text(text).font(.caption.weight(.medium))
            .foregroundStyle(positive ? brand : .secondary)
            .padding(.horizontal, 9).padding(.vertical, 5)
            .background(positive ? brand.opacity(0.12) : Color.secondary.opacity(0.10), in: Capsule())
    }
}

private extension View {
    func card() -> some View {
        background(Color(nsColor: .controlBackgroundColor), in: RoundedRectangle(cornerRadius: 13))
            .overlay { RoundedRectangle(cornerRadius: 13).strokeBorder(.quaternary) }
    }
}

struct AddSheet: View {
    @ObservedObject var model: WorkbenchModel
    @Binding var isPresented: Bool
    @State private var title = ""
    @State private var customer = true
    @FocusState private var focused: Bool

    private var existing: Conversation? {
        let name = title.trimmingCharacters(in: .whitespacesAndNewlines)
        return model.workspace?.sessions.first { $0.title == name }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("添加企微会话").font(.title2.bold())
            TextField("企微聊天顶部的完整会话名", text: $title)
                .textFieldStyle(.roundedBorder).focused($focused)
                .accessibilityIdentifier("session-title")
            if let existing {
                Label("已添加为\(existing.scope)，可继续采集并更新飞书原行。", systemImage: "arrow.uturn.forward")
                    .font(.callout).foregroundStyle(.secondary)
            } else {
                Picker("用途", selection: $customer) {
                    Text("同事测试").tag(false)
                    Text(acquisitionScope).tag(true)
                }.pickerStyle(.segmented)
            }
            HStack {
                Spacer()
                Button("取消") { isPresented = false }.keyboardShortcut(.cancelAction)
                Button(existing == nil ? "添加" : "打开已有会话") {
                    Task { if await model.add(title, customer: customer) { isPresented = false } }
                }.buttonStyle(.borderedProminent).keyboardShortcut(.defaultAction)
                    .disabled(title.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || model.submitting)
            }
        }.padding(24).frame(width: 400).onAppear { focused = true }
    }
}

struct APISettingsSheet: View {
    @ObservedObject var model: WorkbenchModel
    @Environment(\.dismiss) private var dismiss
    @State private var url = ""
    @State private var name = ""
    @State private var key = ""
    @State private var error: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Label("模型 API 配置", systemImage: "key").font(.title2.bold())
            Text("保存后，可在会话的「模型 API」菜单中使用。").foregroundStyle(.secondary)
            VStack(alignment: .leading, spacing: 10) {
                TextField("HTTPS 基础地址", text: $url, prompt: Text("https://api.example.com/v1"))
                    .accessibilityIdentifier("api-base-url")
                TextField("模型名", text: $name)
                    .accessibilityIdentifier("api-model")
                SecureField("API 密钥", text: $key, prompt: Text("输入密钥"))
                    .accessibilityIdentifier("api-key")
            }.textFieldStyle(.roundedBorder)
            if let error { Text(error).font(.callout).foregroundStyle(.red) }
            HStack {
                Spacer()
                Button("取消") { dismiss() }.keyboardShortcut(.cancelAction)
                Button("保存") {
                    Task {
                        do {
                            try await model.saveAPI(url: url.trimmingCharacters(in: .whitespaces),
                                                    model: name.trimmingCharacters(in: .whitespaces),
                                                    key: key.trimmingCharacters(in: .whitespaces))
                            dismiss()
                        } catch { self.error = error.localizedDescription }
                    }
                }.buttonStyle(.borderedProminent).keyboardShortcut(.defaultAction)
                    .disabled(url.isEmpty || name.isEmpty || key.isEmpty || model.submitting)
            }
        }.padding(24).frame(width: 475)
            .onAppear {
                url = model.workspace?.api?.url ?? ""
                name = model.workspace?.api?.model ?? ""
            }
    }
}

struct Confirmation: Identifiable {
    let action: String
    let session: Conversation
    var destination: Destination? = nil
    var id: String { action }
    var icon: String { action == "sync" ? "arrow.up.right.square" : "text.viewfinder" }
    var button: String { action == "sync" ? "确认同步" : "开始处理" }

    var title: String {
        switch action {
        case "sync": return "同步飞书测试表"
        case "local_reprocess": return "重新本地分层"
        case "api_capture", "api_reprocess": return "确认使用模型 API"
        default: return "采集当前企微窗口"
        }
    }

    var explanation: String {
        let window = "请先在企微中打开这条会话，滚到最新消息。首次采集至少显示两条完整文字消息，再次采集请保留上次末尾的消息。"
        let api = "模型服务会接收客户消息文字及最多六条近期上下文。"
        switch action {
        case "sync": return "将当前预览的结构化分层结果写入已配置的飞书测试表。"
        case "local_reprocess": return "使用本机 Ollama 重新处理已采集的消息，更新选科组合等分层字段。"
        case "api_capture": return window + "\n\n" + api
        case "api_reprocess": return api
        default: return window
        }
    }
}
