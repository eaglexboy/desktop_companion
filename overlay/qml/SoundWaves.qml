// What a model with no mouth shows while it talks: a few arcs of growing size beside it, like sound
// leaving it. A wave travels outwards through them (each lights up in turn and fades) and the whole set
// trembles a little, so it reads as a voice. Nothing about the model itself moves.
pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Shapes

Item {
    id: root

    property bool active: false
    // 1 at the default 240 px avatar; everything scales with it.
    property real unit: 1.0
    property color waveColor: "#F2F8FF"
    property int arcCount: 3

    width: (14 + 13 * arcCount) * unit
    height: (26 + 14 * arcCount) * unit
    opacity: active ? 1 : 0
    visible: opacity > 0.01
    Behavior on opacity { NumberAnimation { duration: 140 } }

    // 0..1, once per wave: arc i peaks when the phase reaches i / arcCount.
    property real phase: 0
    NumberAnimation on phase {
        from: 0; to: 1; duration: 640
        loops: Animation.Infinite
        running: root.active
    }
    // -1..1, fast: the tremble.
    property real shake: 0
    SequentialAnimation on shake {
        loops: Animation.Infinite
        running: root.active
        NumberAnimation { to: 1; duration: 48 }
        NumberAnimation { to: -1; duration: 48 }
    }

    // How lit arc `index` is: brightest as the wave passes it, then dying away until the next one.
    function lit(index) {
        const d = (((root.phase - index / root.arcCount) % 1) + 1) % 1
        return 0.22 + 0.78 * Math.max(0, 1 - d * 1.7)
    }

    Repeater {
        model: root.arcCount
        delegate: Shape {
            id: arc
            required property int index
            // Arcs open to the right and grow outwards from the left edge of this item.
            readonly property real radius: (9 + index * 13) * root.unit
            readonly property real strength: root.lit(index)
            x: root.shake * (0.5 + index * 0.45) * root.unit
            width: root.width
            height: root.height
            opacity: strength
            scale: 0.94 + 0.08 * strength
            transformOrigin: Item.Left
            preferredRendererType: Shape.CurveRenderer

            // A dark edge under the light line keeps the arcs legible on a pale background.
            ShapePath {
                strokeColor: "#99000000"
                strokeWidth: 5.6 * root.unit
                fillColor: "transparent"
                capStyle: ShapePath.RoundCap
                PathAngleArc {
                    centerX: 0; centerY: root.height / 2
                    radiusX: arc.radius; radiusY: arc.radius
                    startAngle: -42; sweepAngle: 84
                }
            }
            ShapePath {
                strokeColor: root.waveColor
                strokeWidth: 3 * root.unit
                fillColor: "transparent"
                capStyle: ShapePath.RoundCap
                PathAngleArc {
                    centerX: 0; centerY: root.height / 2
                    radiusX: arc.radius; radiusY: arc.radius
                    startAngle: -42; sweepAngle: 84
                }
            }
        }
    }
}
