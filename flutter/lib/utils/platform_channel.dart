import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';
import 'package:flutter_hbb/main.dart';
import 'package:flutter_hbb/common.dart';

enum SystemWindowTheme { light, dark }

/// The platform channel for RustDesk.
class RdPlatformChannel {
  RdPlatformChannel._();

  static final RdPlatformChannel _windowUtil = RdPlatformChannel._();

  static RdPlatformChannel get instance => _windowUtil;

  final MethodChannel _hostMethodChannel =
      MethodChannel("org.rustdesk.rustdesk/host");

  /// Bump the position of the mouse cursor, if applicable
  Future<bool> bumpMouse({required int dx, required int dy}) async {
    // No debug output; this call is too chatty.

    bool? result = await _hostMethodChannel
      .invokeMethod("bumpMouse", {"dx": dx, "dy": dy});

    return result ?? false;
  }

  /// Change the theme of the system window
  Future<void> changeSystemWindowTheme(SystemWindowTheme theme) {
    assert(isMacOS);
    if (kDebugMode) {
      print(
          "[Window ${kWindowId ?? 'Main'}] change system window theme to ${theme.name}");
    }
    return _hostMethodChannel
        .invokeMethod("setWindowTheme", {"themeName": theme.name});
  }

  /// Set the calling window's overall opacity.
  /// [opacity] is clamped to [0.0, 1.0] on the native side. macOS only.
  /// Safe to call from any window isolate: the native handler resolves to the
  /// NSWindow that owns the current engine (main window or a remote sub-window).
  Future<void> setWindowOpacity(double opacity) {
    assert(isMacOS);
    return _hostMethodChannel
        .invokeMethod("setWindowOpacity", {"opacity": opacity});
  }

  /// Terminate .app manually.
  Future<void> terminate() {
    assert(isMacOS);
    return _hostMethodChannel.invokeMethod("terminate");
  }
}
