cmake_minimum_required(VERSION 3.21)
project(deskmate_overlay LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_AUTOMOC ON)

find_package(Qt6 REQUIRED COMPONENTS Core Gui Widgets Qml Quick Quick3D Quick3DAssetUtils QuickControls2 WebSockets Multimedia Test)
# The LayerShellQt CMake package provides the target LayerShellQt::Interface.
find_package(LayerShellQt REQUIRED)
# KF6GlobalAccel: the documented C++ API shortcut_manager.cpp uses to
# register global hotkeys with org.kde.kglobalaccel, in place of hand-rolling
# raw QDBusInterface calls against doRegister/setShortcutKeys and guessing at
# their undocumented flag semantics (see shortcut_manager.h).
find_package(KF6GlobalAccel REQUIRED)

qt_standard_project_setup(REQUIRES 6.5)

include(FetchContent)
# An immediate fallback source if Qt's RuntimeLoader exhibits driver-level
# morph/animation-blending defects. Not linked into any target below --
# kept declared, not wired up, so it's one `target_link_libraries` line
# away from being used if a given driver stack ever needs it.
FetchContent_Declare(tinygltf
    GIT_REPOSITORY https://github.com/syoyo/tinygltf.git
    GIT_TAG v2.9.3
)
FetchContent_Populate(tinygltf)

qt_add_executable(deskmate_overlay
    src/main.cpp
    src/viseme_math.cpp
    src/viseme_math.h
    src/avatar_controller.cpp
    src/avatar_controller.h
    src/ipc_client.cpp
    src/ipc_client.h
    src/audio_capture.cpp
    src/audio_capture.h
    src/shortcut_manager.cpp
    src/shortcut_manager.h
    src/window_mask_helper.cpp
    src/window_mask_helper.h
    src/layer_shell_screen_helper.cpp
    src/layer_shell_screen_helper.h
    src/glb_sanitizer.cpp
    src/glb_sanitizer.h
)

qt_add_qml_module(deskmate_overlay
    URI Deskmate
    VERSION 1.0
    QML_FILES
        qml/Main.qml
        qml/Avatar3D.qml
        qml/InteractionBar.qml
        qml/SpeechBubble.qml
        qml/AppearanceStatus.qml
        qml/IdleBehaviors.qml
    RESOURCES
        assets/placeholder/look.glb
)

# Required for qt_add_qml_module's generated qmltyperegistrations.cpp,
# which includes each QML_ELEMENT header via angle brackets
# (__has_include(<avatar_controller.h>)) -- those only resolve through the
# compiler's include *search path*, not source-relative paths, so src/
# must be on it.
target_include_directories(deskmate_overlay PRIVATE src)

target_link_libraries(deskmate_overlay PRIVATE
    Qt6::Core
    Qt6::Gui
    Qt6::Widgets
    Qt6::Qml
    Qt6::Quick
    Qt6::Quick3D
    Qt6::Quick3DAssetUtils
    Qt6::QuickControls2
    Qt6::WebSockets
    Qt6::Multimedia
    LayerShellQt::Interface
    KF6::GlobalAccel
)

# `assets/placeholder/look.glb` is the seed character, generated via the
# Blender pipeline:
#   blender --background --factory-startup --python ../blender/generator.py -- \
#     --spec ../examples/sample_look.json --out assets/placeholder/look.glb

enable_testing()
add_subdirectory(tests)
