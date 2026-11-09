import SwiftUI
import ShellUI
import S2ArmShellBridge

/// 库界面 = 上游 LibraryView 挂在我方模型上；出现时开手柄监听、消失时关（Play 后游戏接管输入，两边不同时听）。
public struct S2ArmShellHostView: View {
    let model: S2ArmShellHostModel
    public init(model: S2ArmShellHostModel) { self.model = model }
    public var body: some View {
        LibraryView(games: model.games, controllers: model.controllers, actions: model.actions, input: model.input,
                    padSnapshot: model.padSnapshot)
            .onAppear { model.startControllers(); s2a_shell_library_appeared() }
            .onDisappear { model.stopControllers() }
        #if canImport(UIKit)
            // 回前台重判资源包：玩家去「文件」/ 访达拷完资源包回来，不用重开 App
            .onReceive(NotificationCenter.default.publisher(for: UIApplication.didBecomeActiveNotification)) { _ in model.refreshGameData() }
            // 虚拟手柄布局编辑页（设置里「按键布局 → 调整位置…」）：盖在库界面上，关了回到设置
            .overlay {
                if let id = model.editingLayout {
                    S2ArmTouchEditorView(gameID: id, banner: model.bannerURLs[id],
                                         opacity: (UserDefaults.standard.object(forKey: "S2AVPadOpacity") as? Double).map { min(1, max(0.1, $0)) } ?? 1,
                                         close: { model.closeLayoutEditor() })
                }
                if let id = model.editingIdentity {
                    S2ArmGameIdentityView(title: model.identityName(id), initialName: model.identityName(id), hasCustom: model.hasCustomIdentity(id),
                                          save: { model.saveIdentity(id, name: $0, cover: $1) }, reset: { model.resetIdentity(id) }, close: { model.closeIdentityEditor(id) })
                }
            }
        #endif
    }
}

#if canImport(UIKit)
import UIKit

/// app.m 用的 C 入口（声明在 S2ArmShellBridge/include/s2a_shell.h）：返回 +1 的 UIHostingController。
/// 只在主线程调（头文件约定）；@MainActor 让 Swift 侧按主线程隔离编译。
@MainActor @_cdecl("s2a_shell_library_vc")
public func s2a_shell_library_vc(_ cataloguePath: UnsafePointer<CChar>?) -> UnsafeMutableRawPointer {
    let path = cataloguePath.map { String(cString: $0) } ?? ""
    let model = S2ArmShellHostModel(catalogue: Catalogue.load(path: path))
    // 宿主「重新检查」→ 重判资源包；模型随 vc 活着，登记的是不持有的指针（vc 由 app.m 持有到进程结束）
    s2a_shell_set_refresh_handler({ ctx in
        guard let ctx else { return }
        MainActor.assumeIsolated { Unmanaged<S2ArmShellHostModel>.fromOpaque(ctx).takeUnretainedValue().refreshGameData() }
    }, Unmanaged.passUnretained(model).toOpaque())
    let vc = UIHostingController(rootView: S2ArmShellHostView(model: model))
    return Unmanaged.passRetained(vc as UIViewController).toOpaque()
}
#endif
