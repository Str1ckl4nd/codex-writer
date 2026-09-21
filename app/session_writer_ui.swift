import AppKit
import Foundation
import Darwin

private let env = ProcessInfo.processInfo.environment
private let userHome = FileManager.default.homeDirectoryForCurrentUser
private let bundledBackend = Bundle.main.resourceURL?.appendingPathComponent("backend/session_writer.py").path
private let backendPath = env["SESSION_WRITER_BACKEND"] ?? bundledBackend ?? userHome.appendingPathComponent(".local/share/session-writer/session_writer.py").path
private let logDirectory = URL(fileURLWithPath: env["SESSION_WRITER_STATE_DIR"] ?? userHome.appendingPathComponent(".local/state/session-writer").path).appendingPathComponent("logs").path
private let refreshInterval: TimeInterval = 10

private struct SessionRecord: Codable, Sendable {
    let id: String
    let name: String
    let createdTime: String
    let writerHostName: String?
    let ownerMachineId: String?
    let pid: Int?
    let processKind: String?
    let affectedCount: Int?
    let ownerState: String?
    let status: String?
    let statusLabel: String?
    let diagnosticSummary: String?
}

private struct MachineRecord: Codable, Sendable {
    let id: String
    let name: String
    let status: String
    let statusLabel: String?
    let source: String?
    let selectable: Bool
    let reason: String?
    let clientKind: String?
}

private struct DiagnosticRecord: Codable, Sendable {
    let code: String
    let severity: String
    let title: String
    let detail: String
    let action: String?
}

private struct WriterSnapshot: Codable, Sendable {
    let schemaVersion: Int
    let generatedAt: String
    let records: [SessionRecord]
    let machines: [MachineRecord]
    let diagnostics: [DiagnosticRecord]
    let stale: Bool
    let storageHost: StorageHost?
}

private struct StorageHost: Codable, Sendable { let name: String }

private struct HandoffPlan: Codable, Sendable {
    let currentPid: Int?
    let message: String
    let revision: String?
    let canApply: Bool
    let reason: String?
}

private struct ClaimResult: Codable, Sendable {
    let ok: Bool
    let message: String
    let continuationComplete: Bool?
}

private enum UIError: Error, LocalizedError {
    case backend(String)
    var errorDescription: String? {
        switch self {
        case .backend(let message): return message
        }
    }
}

private final class CapturedData: @unchecked Sendable {
    private let lock = NSLock()
    private var storage = Data()
    func set(_ data: Data) {
        lock.lock()
        storage = data
        lock.unlock()
    }
    var data: Data {
        lock.lock()
        defer { lock.unlock() }
        return storage
    }
}

// Runs off the UI thread. Drain both pipes while the child runs.
private func runBackend(_ arguments: [String], timeout: TimeInterval = 75) throws -> Data {
    let process = Process()
    let stdout = Pipe()
    let stderr = Pipe()
    let exited = DispatchSemaphore(value: 0)
    let readers = DispatchGroup()
    let output = CapturedData()
    let errorOutput = CapturedData()
    process.executableURL = URL(fileURLWithPath: env["SESSION_WRITER_PYTHON"] ?? "/usr/bin/python3")
    let client = try JSONSerialization.data(withJSONObject: ["schemaVersion": 1, "platform": "mac"])
    process.arguments = [backendPath] + arguments + ["--client-context", client.base64EncodedString()]
    process.standardInput = FileHandle.nullDevice
    process.standardOutput = stdout
    process.standardError = stderr
    process.terminationHandler = { _ in exited.signal() }
    try process.run()
    readers.enter()
    DispatchQueue.global(qos: .userInitiated).async {
        output.set(stdout.fileHandleForReading.readDataToEndOfFile())
        readers.leave()
    }
    readers.enter()
    DispatchQueue.global(qos: .userInitiated).async {
        errorOutput.set(stderr.fileHandleForReading.readDataToEndOfFile())
        readers.leave()
    }
    if exited.wait(timeout: .now() + timeout) == .timedOut {
        process.terminate()
        if exited.wait(timeout: .now() + 2) == .timedOut {
            kill(process.processIdentifier, SIGKILL)
            _ = exited.wait(timeout: .now() + 2)
        }
        _ = readers.wait(timeout: .now() + 2)
        throw UIError.backend("请求超时（\(Int(timeout)) 秒）。请检查网络连接，然后点击“刷新 SSH 主机”。")
    }
    guard readers.wait(timeout: .now() + 5) == .success else {
        throw UIError.backend("服务已结束，但结果未完整返回。请刷新后再试。")
    }
    guard process.terminationStatus == 0 else {
        if let object = try? JSONSerialization.jsonObject(with: output.data) as? [String: Any],
           let message = (object["message"] ?? object["error"]) as? String {
            throw UIError.backend(message)
        }
        let resultText = String(data: output.data, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        let errorText = String(data: errorOutput.data, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        let message = resultText.isEmpty ? errorText : resultText
        throw UIError.backend(message.isEmpty ? "服务未返回可用结果（退出码 \(process.terminationStatus)）。" : String(message.prefix(3000)))
    }
    return output.data
}

private func loadSnapshot(rediscover: Bool = false, fixturePath: String? = nil) throws -> WriterSnapshot {
    let data: Data
    if let fixturePath {
        data = try Data(contentsOf: URL(fileURLWithPath: fixturePath))
    } else {
        var arguments = ["snapshot", "--format", "json"]
        if rediscover { arguments.append("--rediscover") }
        data = try runBackend(arguments)
    }
    let snapshot = try JSONDecoder().decode(WriterSnapshot.self, from: data)
    guard snapshot.schemaVersion == 4 else {
        throw UIError.backend("界面和后台版本不一致，请更新后重试。")
    }
    return snapshot
}

@MainActor
private final class PanelContentView: NSView {
    override func draw(_ dirtyRect: NSRect) {
        NSColor.windowBackgroundColor.setFill()
        dirtyRect.fill()
        super.draw(dirtyRect)
    }
}

@MainActor
private final class WriterWindowController: NSObject, NSApplicationDelegate, NSWindowDelegate, NSTableViewDataSource, NSTableViewDelegate {
    private let fixturePath: String?
    private var snapshot: WriterSnapshot?
    private var sessions: [SessionRecord] = []
    private var machines: [MachineRecord] = []
    private var selectedSessionID: String?
    private var selectedMachineID: String?
    private var userSelectedMachine = false
    private var rendering = false
    private var refreshing = false
    private var applying = false
    private var stale = true
    private var lastError: String?
    private var timer: Timer?
    private var window: NSWindow!
    private var tableView: NSTableView!
    private var hostPopup: NSPopUpButton!
    private var currentOwnerLabel: NSTextField!
    private var contextLabel: NSTextField!
    private var currentStateLabel: NSTextField!
    private var targetReasonLabel: NSTextField!
    private var refreshTimeLabel: NSTextField!
    private var footerLabel: NSTextField!
    private var autoRefreshButton: NSButton!
    private var refreshButton: NSButton!
    private var rediscoverButton: NSButton!
    private var applyButton: NSButton!
    private var cancelButton: NSButton!
    private var diagnosticButton: NSButton!
    private var openLogsButton: NSButton!
    private var forceContinueButton: NSButton!
    private var diagnosticScroll: NSScrollView!
    private var diagnosticText: NSTextView!
    private var progress: NSProgressIndicator!

    init(fixturePath: String? = nil) {
        self.fixturePath = fixturePath
        super.init()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        buildWindow()
        window.makeKeyAndOrderFront(nil)
        NSApplication.shared.activate(ignoringOtherApps: true)
        timer = Timer.scheduledTimer(withTimeInterval: refreshInterval, repeats: true) { [weak self] _ in
            Task { @MainActor [weak self] in
                guard let self, self.autoRefreshButton.state == .on else { return }
                self.refresh()
            }
        }
        refresh()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
    func applicationWillTerminate(_ notification: Notification) { timer?.invalidate() }
    func windowShouldClose(_ sender: NSWindow) -> Bool {
        if applying { NSSound.beep(); return false }
        return true
    }

    private func label(_ text: String, secondary: Bool = false) -> NSTextField {
        let field = NSTextField(labelWithString: text)
        field.font = NSFont.systemFont(ofSize: 13)
        field.textColor = secondary ? .secondaryLabelColor : .labelColor
        field.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        field.isSelectable = true
        return field
    }

    private func row(_ views: [NSView], spacing: CGFloat = 12) -> NSStackView {
        let stack = NSStackView(views: views)
        stack.orientation = .horizontal
        stack.alignment = .centerY
        stack.spacing = spacing
        return stack
    }

    private func spacer() -> NSView {
        let view = NSView()
        view.setContentHuggingPriority(.defaultLow, for: .horizontal)
        return view
    }

    private func buildWindow() {
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1140, height: 835),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = "会话写入者"
        window.minSize = NSSize(width: 1120, height: 820)
        window.delegate = self
        window.center()
        window.contentView = PanelContentView(frame: window.contentView!.bounds)
        let content = window.contentView!
        let stack = NSStackView()
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 12
        stack.translatesAutoresizingMaskIntoConstraints = false
        content.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 20),
            stack.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -20),
            stack.topAnchor.constraint(equalTo: content.topAnchor, constant: 18),
            stack.bottomAnchor.constraint(equalTo: content.bottomAnchor, constant: -18),
        ])
        autoRefreshButton = NSButton(checkboxWithTitle: "自动刷新（每 10 秒）", target: self, action: #selector(toggleAutoRefresh))
        autoRefreshButton.state = .on
        refreshButton = NSButton(title: "刷新", target: self, action: #selector(manualRefresh))
        rediscoverButton = NSButton(title: "刷新 SSH 主机", target: self, action: #selector(rediscoverMachines))
        refreshTimeLabel = label("尚未更新", secondary: true)
        progress = NSProgressIndicator()
        progress.style = .spinning
        progress.controlSize = .small
        progress.isDisplayedWhenStopped = false
        progress.widthAnchor.constraint(equalToConstant: 18).isActive = true
        let toolbar = row([autoRefreshButton, spacer(), progress, refreshTimeLabel, refreshButton, rediscoverButton])
        stack.addArrangedSubview(toolbar)
        toolbar.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
        contextLabel = label("操作端：本机 Mac · 会话数据：正在读取…", secondary: true)
        contextLabel.setAccessibilityIdentifier("hostContext")
        stack.addArrangedSubview(contextLabel)
        stack.addArrangedSubview(label("选择会话"))
        tableView = NSTableView()
        tableView.delegate = self
        tableView.dataSource = self
        tableView.allowsMultipleSelection = false
        tableView.allowsEmptySelection = true
        tableView.usesAlternatingRowBackgroundColors = true
        tableView.rowHeight = 28
        tableView.columnAutoresizingStyle = .lastColumnOnlyAutoresizingStyle
        let columns: [(String, String, CGFloat)] = [("name", "会话名称", 250), ("id", "会话 ID", 340), ("created", "创建时间", 220), ("owner", "当前占用者", 270)]
        for (identifier, title, width) in columns {
            let column = NSTableColumn(identifier: NSUserInterfaceItemIdentifier(identifier))
            column.title = title
            column.width = width
            column.minWidth = identifier == "id" ? 340 : 180
            tableView.addTableColumn(column)
        }
        let tableScroll = NSScrollView()
        tableScroll.borderType = .bezelBorder
        tableScroll.hasVerticalScroller = true
        tableScroll.hasHorizontalScroller = true
        tableScroll.documentView = tableView
        stack.addArrangedSubview(tableScroll)
        tableScroll.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
        tableScroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 230).isActive = true
        tableScroll.setContentHuggingPriority(.defaultLow, for: .vertical)
        currentOwnerLabel = label("当前占用者：正在读取…")
        currentOwnerLabel.isHidden = true
        currentStateLabel = label("观测状态：尚未获取", secondary: true)
        stack.addArrangedSubview(currentStateLabel)
        currentStateLabel.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
        currentStateLabel.lineBreakMode = .byTruncatingTail
        hostPopup = NSPopUpButton(frame: .zero, pullsDown: false)
        hostPopup.target = self
        hostPopup.action = #selector(targetChanged)
        hostPopup.autoenablesItems = false
        hostPopup.widthAnchor.constraint(equalToConstant: 370).isActive = true
        targetReasonLabel = label("正在读取本机与 SSH 主机…", secondary: true)
        targetReasonLabel.lineBreakMode = .byTruncatingTail
        let targetRow = row([label("目标写入主机"), hostPopup, targetReasonLabel])
        stack.addArrangedSubview(targetRow)
        targetRow.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
        forceContinueButton = NSButton(checkboxWithTitle: "强制接管并继续被中断代理", target: nil, action: nil)
        forceContinueButton.state = .off
        forceContinueButton.toolTip = "默认关闭。正常关闭后仍持锁时才强制停止已核验服务；仅恢复本次中断的代理，子代理经主代理转交。应用前仍需确认影响名单。"
        forceContinueButton.setAccessibilityIdentifier("forceContinue")
        stack.addArrangedSubview(forceContinueButton)
        diagnosticButton = NSButton(title: "收起排错", target: self, action: #selector(toggleDiagnostics))
        openLogsButton = NSButton(title: "打开日志", target: self, action: #selector(openLogs))
        openLogsButton.toolTip = "打开排障日志文件夹：\(logDirectory)"
        openLogsButton.setAccessibilityIdentifier("openLogs")
        let diagnosticHeader = row([label("基本排错 · SSH 主机"), spacer(), openLogsButton, diagnosticButton])
        stack.addArrangedSubview(diagnosticHeader)
        diagnosticHeader.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
        diagnosticScroll = NSScrollView()
        diagnosticScroll.borderType = .bezelBorder
        diagnosticScroll.hasVerticalScroller = true
        diagnosticText = NSTextView(frame: NSRect(x: 0, y: 0, width: 1080, height: 150))
        diagnosticText.isEditable = false
        diagnosticText.isSelectable = true
        diagnosticText.font = .systemFont(ofSize: 12)
        diagnosticText.textContainerInset = NSSize(width: 9, height: 8)
        diagnosticText.isVerticallyResizable = true
        diagnosticText.isHorizontallyResizable = false
        diagnosticText.autoresizingMask = [.width]
        diagnosticText.textContainer?.widthTracksTextView = true
        diagnosticText.textContainer?.containerSize = NSSize(width: 1062, height: CGFloat.greatestFiniteMagnitude)
        diagnosticScroll.documentView = diagnosticText
        stack.addArrangedSubview(diagnosticScroll)
        diagnosticScroll.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
        diagnosticScroll.heightAnchor.constraint(equalToConstant: 155).isActive = true
        footerLabel = label("正在读取会话和可用机器…", secondary: true)
        footerLabel.lineBreakMode = .byTruncatingTail
        applyButton = NSButton(title: "应用", target: self, action: #selector(applySelection))
        applyButton.keyEquivalent = "\r"
        applyButton.widthAnchor.constraint(equalToConstant: 90).isActive = true
        cancelButton = NSButton(title: "关闭", target: self, action: #selector(cancel))
        cancelButton.keyEquivalent = "\u{1b}"
        cancelButton.widthAnchor.constraint(equalToConstant: 90).isActive = true
        let bottom = row([footerLabel, spacer(), applyButton, cancelButton])
        stack.addArrangedSubview(bottom)
        bottom.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
        updateControls()
    }

    func numberOfRows(in tableView: NSTableView) -> Int { sessions.count }
    func tableView(_ tableView: NSTableView, viewFor tableColumn: NSTableColumn?, row: Int) -> NSView? {
        guard let column = tableColumn, sessions.indices.contains(row) else { return nil }
        let cell = NSTableCellView()
        cell.identifier = column.identifier
        let field = label("")
        field.translatesAutoresizingMaskIntoConstraints = false
        field.lineBreakMode = .byTruncatingTail
        field.maximumNumberOfLines = 1
        field.cell?.wraps = false
        switch column.identifier.rawValue {
        case "name": field.stringValue = sessions[row].name
        case "id":
            field.stringValue = sessions[row].id
            field.font = .monospacedSystemFont(ofSize: 12, weight: .regular)
        case "owner":
            let owner = sessions[row].writerHostName?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
            field.stringValue = owner.isEmpty ? "未确认" : owner
        default: field.stringValue = sessions[row].createdTime
        }
        field.toolTip = field.stringValue
        cell.addSubview(field)
        NSLayoutConstraint.activate([
            field.leadingAnchor.constraint(equalTo: cell.leadingAnchor, constant: 6),
            field.trailingAnchor.constraint(equalTo: cell.trailingAnchor, constant: -6),
            field.centerYAnchor.constraint(equalTo: cell.centerYAnchor),
            field.heightAnchor.constraint(equalToConstant: 20),
        ])
        cell.textField = field
        return cell
    }

    func tableViewSelectionDidChange(_ notification: Notification) {
        guard !rendering else { return }
        let index = tableView.selectedRow
        selectedSessionID = sessions.indices.contains(index) ? sessions[index].id : nil
        if !userSelectedMachine { selectDefaultTarget() }
        updateSelectionDetails()
        updateControls()
    }

    private var selectedSession: SessionRecord? { sessions.first { $0.id == selectedSessionID } }
    private var selectedMachine: MachineRecord? { machines.first { $0.id == selectedMachineID } }

    private func selectDefaultTarget() {
        selectedMachineID = machines.first(where: { $0.id == "local" && $0.selectable })?.id
        selectPopupTarget()
    }

    private func selectPopupTarget() {
        if let id = selectedMachineID,
           let item = hostPopup.itemArray.first(where: { $0.representedObject as? String == id }) {
            hostPopup.select(item)
        } else {
            hostPopup.select(nil)
        }
    }

    private func render(_ newSnapshot: WriterSnapshot) {
        rendering = true
        defer { rendering = false }
        snapshot = newSnapshot
        sessions = newSnapshot.records
        machines = newSnapshot.machines
        let dataHost = newSnapshot.storageHost?.name ?? machines.first(where: { $0.id == "local" })?.name ?? "未确认"
        contextLabel.stringValue = "操作端：\(dataHost)（本机 Mac） · 会话数据：\(dataHost)（Mac）"
        stale = newSnapshot.stale
        lastError = nil
        tableView.reloadData()
        if selectedSessionID == nil { selectedSessionID = sessions.first?.id }
        if let index = sessions.firstIndex(where: { $0.id == selectedSessionID }) {
            tableView.selectRowIndexes(IndexSet(integer: index), byExtendingSelection: false)
        } else {
            tableView.deselectAll(nil)
        }
        hostPopup.removeAllItems()
        for machine in machines {
            let title = machine.selectable ? machine.name : "\(machine.name)（\(machine.statusLabel ?? machine.status)）"
            hostPopup.addItem(withTitle: title)
            let item = hostPopup.lastItem!
            item.representedObject = machine.id
            item.isEnabled = machine.selectable
            item.toolTip = machine.reason
        }
        if let oldTarget = selectedMachineID, !machines.contains(where: { $0.id == oldTarget }) {
            hostPopup.addItem(withTitle: "原目标暂不可用，请重新选择")
            hostPopup.lastItem?.representedObject = oldTarget
            hostPopup.lastItem?.isEnabled = false
        }
        if selectedMachineID == nil { selectDefaultTarget() }
        selectPopupTarget()
        let dateFormatter = DateFormatter()
        dateFormatter.dateFormat = "HH:mm:ss"
        let parser = ISO8601DateFormatter()
        parser.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let fractionalDate = parser.date(from: newSnapshot.generatedAt)
        parser.formatOptions = [.withInternetDateTime]
        if let date = fractionalDate ?? parser.date(from: newSnapshot.generatedAt) {
            refreshTimeLabel.stringValue = "更新于 \(dateFormatter.string(from: date))"
        } else {
            refreshTimeLabel.stringValue = "更新于 \(newSnapshot.generatedAt)"
        }
        refreshTimeLabel.toolTip = newSnapshot.generatedAt
        updateSelectionDetails()
        updateDiagnostics()
        updateControls()
    }

    private func updateSelectionDetails() {
        if let session = selectedSession {
            let owner = session.writerHostName?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
            currentOwnerLabel.stringValue = "当前占用者：\(owner.isEmpty ? "未确认" : owner)"
            let state = session.statusLabel ?? session.ownerState ?? session.status ?? "未确认"
            let detail = session.diagnosticSummary ?? ""
            currentStateLabel.stringValue = "观测状态：\(state)\(detail.isEmpty ? "" : " · " + detail)\(stale ? "（数据已过期）" : "")"
            currentStateLabel.toolTip = currentStateLabel.stringValue
        } else {
            currentOwnerLabel.stringValue = "当前占用者：请先选择会话"
            currentStateLabel.stringValue = sessions.isEmpty ? "观测状态：当前列表没有会话，请查看下方排错信息。" : "观测状态：请从列表选择会话。"
        }
        if let machine = selectedMachine {
            let state = machine.statusLabel ?? machine.status
            let reason = machine.reason ?? ""
            targetReasonLabel.stringValue = reason.isEmpty ? state : "\(state) · \(reason)"
        } else {
            targetReasonLabel.stringValue = machines.isEmpty ? "尚未发现可用机器" : "请重新选择可用机器"
        }
        targetReasonLabel.toolTip = targetReasonLabel.stringValue
    }

    private func updateDiagnostics() {
        var lines: [String] = []
        if let error = lastError {
            lines += ["刷新失败：\(error)", "已保留上次数据；重新成功读取前，“应用”不可用。", ""]
        }
        if snapshot?.stale == true { lines += ["部分信息已过期；请先处理连接问题并刷新。", ""] }
        if !machines.isEmpty {
            lines.append("本机与已登记 SSH 主机（\(machines.count)）")
            for machine in machines {
                let state = machine.statusLabel ?? machine.status
                let reason = machine.reason.flatMap { $0.isEmpty ? nil : $0 }.map { "；\($0)" } ?? ""
                lines.append("• \(machine.name)：\(state)\(reason)")
            }
            lines.append("")
        }
        for diagnostic in snapshot?.diagnostics ?? [] {
            let prefix = diagnostic.severity == "error" ? "需要处理" : diagnostic.severity == "warning" ? "注意" : "检查"
            lines.append("\(prefix) · \(diagnostic.title)：\(diagnostic.detail)")
            if let action = diagnostic.action, !action.isEmpty { lines.append("  建议：\(action)") }
        }
        if lines.isEmpty {
            lines = [snapshot == nil ? "正在检查当前占用者、机器连接和服务状态…" : "本次检查没有报告异常。"]
        }
        if fixturePath != nil { lines.insert("界面预览：使用保存的快照，“应用”已禁用。", at: 0) }
        diagnosticText.string = lines.joined(separator: "\n")
    }

    private func updateControls() {
        guard applyButton != nil else { return }
        let busy = refreshing || applying
        refreshButton.isEnabled = !busy
        rediscoverButton.isEnabled = !busy && fixturePath == nil
        hostPopup.isEnabled = !applying && !machines.isEmpty
        forceContinueButton?.isEnabled = !busy && fixturePath == nil
        tableView.isEnabled = !applying
        cancelButton.isEnabled = !applying
        applyButton.isEnabled = !busy && !stale && selectedSession != nil && selectedMachine?.selectable == true && fixturePath == nil
        if busy { progress.startAnimation(nil) } else { progress.stopAnimation(nil) }
        if applying {
            footerLabel.stringValue = "正在处理交接，自动刷新已暂停…"
            footerLabel.textColor = .secondaryLabelColor
        } else if stale {
            footerLabel.stringValue = snapshot == nil && lastError == nil ? "正在读取会话和可用机器…" : "数据不可确认 · 请查看排错信息并刷新"
            footerLabel.textColor = .systemRed
        } else if refreshing {
            footerLabel.stringValue = "正在刷新…"
            footerLabel.textColor = .secondaryLabelColor
        } else {
            let selectableCount = machines.filter(\.selectable).count
            footerLabel.stringValue = "\(sessions.count) 个会话 · \(selectableCount)/\(machines.count) 台机器可选\(autoRefreshButton.state == .on ? " · 自动刷新已开启" : " · 自动刷新已暂停")"
            footerLabel.textColor = .secondaryLabelColor
        }
        footerLabel.toolTip = footerLabel.stringValue
    }

    private func refresh(rediscover: Bool = false) {
        guard !refreshing, !applying else { return }
        refreshing = true
        updateControls()
        let fixture = fixturePath
        Task { [weak self] in
            let result = await Task.detached(priority: .userInitiated) {
                Result { try loadSnapshot(rediscover: rediscover, fixturePath: fixture) }
            }.value
            guard let self else { return }
            self.refreshing = false
            switch result {
            case .success(let snapshot): self.render(snapshot)
            case .failure(let error): self.recordRefreshFailure(error.localizedDescription)
            }
        }
    }

    private func recordRefreshFailure(_ message: String) {
        stale = true
        lastError = message
        diagnosticScroll.isHidden = false
        diagnosticButton.title = "收起排错"
        updateSelectionDetails()
        updateDiagnostics()
        updateControls()
    }

    @objc private func manualRefresh() { refresh() }
    @objc private func rediscoverMachines() { refresh(rediscover: true) }
    @objc private func toggleAutoRefresh() { updateControls() }
    @objc private func targetChanged() {
        selectedMachineID = hostPopup.selectedItem?.representedObject as? String
        userSelectedMachine = true
        updateSelectionDetails()
        updateControls()
    }
    @objc private func toggleDiagnostics() {
        diagnosticScroll.isHidden.toggle()
        diagnosticButton.title = diagnosticScroll.isHidden ? "展开排错" : "收起排错"
    }
    @objc private func openLogs() {
        let url = URL(fileURLWithPath: logDirectory, isDirectory: true)
        do {
            try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
            if !NSWorkspace.shared.open(url) {
                showAlert(title: "无法打开日志", message: "请在 Finder 中打开：\n\(logDirectory)", style: .warning)
            }
        } catch {
            showAlert(title: "无法打开日志", message: error.localizedDescription, style: .warning)
        }
    }
    @objc private func cancel() {
        guard !applying else { return }
        NSApplication.shared.terminate(nil)
    }

    @objc private func applySelection() {
        guard applyButton.isEnabled, let session = selectedSession, let machine = selectedMachine else { return }
        applying = true
        let forceContinue = forceContinueButton.state == .on
        let forceArguments = forceContinue ? ["--force-continue"] : []
        updateControls()
        Task { [weak self] in
            let result = await Task.detached(priority: .userInitiated) {
                Result {
                    let data = try runBackend(["plan", "--thread-id", session.id, "--machine-id", machine.id, "--format", "json"] + forceArguments, timeout: 120)
                    return try JSONDecoder().decode(HandoffPlan.self, from: data)
                }
            }.value
            guard let self else { return }
            switch result {
            case .failure(let error): self.finishHandoff(error: error.localizedDescription)
            case .success(let plan):
                guard plan.canApply, let revision = plan.revision, !revision.isEmpty else {
                    self.showAlert(title: "暂时无法应用", message: plan.reason ?? plan.message, style: .warning)
                    self.applying = false
                    self.updateControls()
                    self.refresh()
                    return
                }
                let confirmation = NSAlert()
                confirmation.messageText = forceContinue ? "确认强制接管与代理恢复" : "应用写入主机"
                confirmation.informativeText = "会话：\(session.name)\n目标主机：\(machine.name)\n\n\(plan.message)"
                confirmation.alertStyle = .warning
                confirmation.addButton(withTitle: "应用")
                confirmation.addButton(withTitle: "取消")
                guard confirmation.runModal() == .alertFirstButtonReturn else {
                    self.applying = false
                    self.updateControls()
                    self.refresh()
                    return
                }
                let claimResult = await Task.detached(priority: .userInitiated) {
                    Result {
                        let data = try runBackend(["claim", "--thread-id", session.id, "--machine-id", machine.id,
                                                   "--expected-revision", revision, "--format", "json"] + forceArguments, timeout: 240)
                        return try JSONDecoder().decode(ClaimResult.self, from: data)
                    }
                }.value
                switch claimResult {
                case .failure(let error): self.finishHandoff(error: error.localizedDescription)
                case .success(let claim):
                    if claim.ok {
                        self.showAlert(title: "交接结果", message: claim.message,
                                       style: claim.continuationComplete == false ? .warning : .informational)
                        self.forceContinueButton.state = .off
                        self.applying = false
                        self.updateControls()
                        self.refresh()
                    } else { self.finishHandoff(error: claim.message) }
                }
            }
        }
    }

    private func finishHandoff(error: String) {
        stale = true
        lastError = "交接未确认完成：\(error)"
        updateSelectionDetails()
        updateDiagnostics()
        diagnosticScroll.isHidden = false
        diagnosticButton.title = "收起排错"
        showAlert(title: "交接未确认完成", message: error, style: .critical)
        applying = false
        updateControls()
        refresh()
    }

    private func showAlert(title: String, message: String, style: NSAlert.Style = .informational) {
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = message
        alert.alertStyle = style
        alert.addButton(withTitle: "好")
        alert.runModal()
    }

    // Build the same native controls as the visible window; never plan or claim.
    func selfTest(snapshot: WriterSnapshot, previewPath: String? = nil) throws {
        buildWindow()
        render(snapshot)
        window.contentView?.layoutSubtreeIfNeeded()
        let firstSession = selectedSessionID
        if let lastMachine = machines.last(where: \.selectable) {
            selectedMachineID = lastMachine.id
            userSelectedMachine = true
            selectPopupTarget()
        }
        let chosenMachine = selectedMachineID
        render(snapshot)
        let retainedIDs = sessions.map(\.id)
        recordRefreshFailure("界面检查：连接暂不可用")
        let failureRetainsRows = retainedIDs == sessions.map(\.id)
        let failureMarksStale = stale && (selectedSession == nil || currentStateLabel.stringValue.contains("数据已过期"))
        let failureDisablesApply = !applyButton.isEnabled
        render(snapshot)
        window.contentView?.layoutSubtreeIfNeeded()
        if let previewPath, let content = window.contentView {
            // Realize AppKit's backing layers without showing or activating a window.
            window.alphaValue = 0
            window.setFrameOrigin(NSPoint(x: -20000, y: -20000))
            window.orderBack(nil)
            window.displayIfNeeded()
            RunLoop.current.run(until: Date().addingTimeInterval(0.1))
            guard let bitmap = content.bitmapImageRepForCachingDisplay(in: content.bounds) else {
                throw UIError.backend("无法获取界面预览。")
            }
            content.cacheDisplay(in: content.bounds, to: bitmap)
            guard let data = bitmap.representation(using: .png, properties: [:]) else {
                throw UIError.backend("无法生成界面预览图。")
            }
            try data.write(to: URL(fileURLWithPath: previewPath))
        }
        let result: [String: Any] = [
            "title": window.title,
            "columns": tableView.tableColumns.map(\.title),
            "columnWidths": tableView.tableColumns.map(\.width),
            "rows": tableView.numberOfRows,
            "sessionNames": sessions.map(\.name),
            "hosts": machines.map(\.name),
            "machineItems": hostPopup.itemArray.map { ["title": $0.title, "enabled": $0.isEnabled, "id": $0.representedObject as? String ?? ""] as [String: Any] },
            "currentOwner": currentOwnerLabel.stringValue,
            "ownerColumnValues": sessions.map { $0.writerHostName ?? "未确认" },
            "ownerBelowTableVisible": !currentOwnerLabel.isHidden,
            "observedState": currentStateLabel.stringValue,
            "autoRefreshSeconds": refreshInterval,
            "autoRefreshEnabled": autoRefreshButton.state == .on,
            "refreshButton": refreshButton.title,
            "rediscoverButton": rediscoverButton.title,
            "lastRefresh": refreshTimeLabel.stringValue,
            "diagnosticPaneVisible": !diagnosticScroll.isHidden,
            "openLogsButton": openLogsButton.title,
            "logDirectory": logDirectory,
            "diagnostics": diagnosticText.string,
            "applyButton": applyButton.title,
            "applyEnabled": applyButton.isEnabled,
            "fixtureReadOnly": fixturePath != nil,
            "selectionPreserved": firstSession == selectedSessionID && chosenMachine == selectedMachineID,
            "refreshFailureRetainsRows": failureRetainsRows,
            "refreshFailureMarksStale": failureMarksStale,
            "refreshFailureDisablesApply": failureDisablesApply,
            "nativeControlsBuilt": true,
            "mutationsPerformed": false,
        ]
        let data = try JSONSerialization.data(withJSONObject: result, options: [.sortedKeys])
        print(String(data: data, encoding: .utf8)!)
        window.close()
    }
}

@main
@MainActor
private struct WriterAppMain {
    static func main() {
        let arguments = CommandLine.arguments
        var fixturePath: String?
        var previewPath: String?
        if let index = arguments.firstIndex(of: "--snapshot-file") {
            guard arguments.indices.contains(index + 1) else {
                fputs("--snapshot-file 需要文件路径\n", stderr)
                exit(1)
            }
            fixturePath = arguments[index + 1]
        }
        if let index = arguments.firstIndex(of: "--render-file") {
            guard arguments.contains("--self-test"), arguments.indices.contains(index + 1) else {
                fputs("--render-file 仅用于 --self-test，并需要文件路径\n", stderr)
                exit(1)
            }
            previewPath = arguments[index + 1]
        }
        let app = NSApplication.shared
        let controller = WriterWindowController(fixturePath: fixturePath)
        if arguments.contains("--self-test") {
            app.setActivationPolicy(.prohibited)
            do { try controller.selfTest(snapshot: loadSnapshot(fixturePath: fixturePath), previewPath: previewPath) }
            catch {
                fputs("\(error.localizedDescription)\n", stderr)
                exit(1)
            }
            return
        }
        app.setActivationPolicy(.regular)
        app.delegate = controller
        withExtendedLifetime(controller) { app.run() }
    }
}
