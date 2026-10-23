# Avatar-controller morph-target application and asset-swap handling are
# NOT covered by automated Qt Test binaries here -- both need a real
# QQuick3DNode scene graph with actual loaded morph targets to exercise
# meaningfully, which means a real RuntimeLoader + real glTF + real
# Quick3D render context, not something a lightweight offscreen Qt Test
# binary can stand up cheaply. Covered instead by manual verification
# against the compiled app.
#
# What IS covered here, as real automated Qt Test binaries:
# - test_viseme_math: the pure weight-envelope math (viseme_math.cpp),
#   zero Qt GUI dependency.
# - test_window_mask_helper: WindowMaskHelper's real QWindow::setMask()
#   call, against a real (offscreen-platform) QQuickWindow.
# - test_ipc_client: the full hello/welcome handshake and every event
#   IpcClient sends/receives, against a fake brain (QWebSocketServer)
#   standing in for the real brain daemon's WebSocket server.

qt_add_executable(test_viseme_math test_viseme_math.cpp ../src/viseme_math.cpp)
target_link_libraries(test_viseme_math PRIVATE Qt6::Core Qt6::Test)
add_test(NAME test_viseme_math COMMAND test_viseme_math)

qt_add_executable(test_window_mask_helper test_window_mask_helper.cpp ../src/window_mask_helper.cpp)
target_link_libraries(test_window_mask_helper PRIVATE Qt6::Core Qt6::Gui Qt6::Qml Qt6::Quick Qt6::Test)
add_test(NAME test_window_mask_helper COMMAND test_window_mask_helper)
set_tests_properties(test_window_mask_helper PROPERTIES ENVIRONMENT "QT_QPA_PLATFORM=offscreen")

qt_add_executable(test_ipc_client test_ipc_client.cpp ../src/ipc_client.cpp)
target_link_libraries(test_ipc_client PRIVATE Qt6::Core Qt6::Network Qt6::WebSockets Qt6::Test)
add_test(NAME test_ipc_client COMMAND test_ipc_client)

# test_glb_sanitizer: GlbSanitizer's pure GLB rewrite (animations removed,
# all other chunks preserved, bad input rejected) and its cache-file wrapper.
qt_add_executable(test_glb_sanitizer test_glb_sanitizer.cpp ../src/glb_sanitizer.cpp)
target_link_libraries(test_glb_sanitizer PRIVATE Qt6::Core Qt6::Qml Qt6::Test)
add_test(NAME test_glb_sanitizer COMMAND test_glb_sanitizer)
