import QtQuick
import QtQuick.Shapes

// A small line icon drawn from vector paths (24 x 24 grid), in one color:
// no icon font is needed on the login screen.
Item {
    id: icon

    property string name: ""
    property color color: "white"
    property real size: 20

    width: size
    height: size

    // SVG path data on a 24 x 24 grid; `fill` paths are filled, the others stroked
    readonly property var shapes: ({
        "lock": [{ d: "M7 11V8a5 5 0 0 1 10 0v3", fill: false },
                 { d: "M5.5 11h13a1 1 0 0 1 1 1v8a1 1 0 0 1-1 1h-13a1 1 0 0 1-1-1v-8a1 1 0 0 1 1-1z", fill: true }],
        "next": [{ d: "M10 6l6 6-6 6", fill: false }],
        "user": [{ d: "M12 12.5a4.25 4.25 0 1 0 0-8.5 4.25 4.25 0 0 0 0 8.5z", fill: false },
                 { d: "M4.5 20.5a7.5 7.5 0 0 1 15 0", fill: false }],
        "session": [{ d: "M3.5 5h17a1 1 0 0 1 1 1v10a1 1 0 0 1-1 1h-17a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1z", fill: false },
                    { d: "M8 20.5h8M12 17v3.5", fill: false }],
        "moon": [{ d: "M20 14.6A8.3 8.3 0 0 1 9.4 4a8.3 8.3 0 1 0 10.6 10.6z", fill: false }],
        "sun": [{ d: "M12 16a4 4 0 1 0 0-8 4 4 0 0 0 0 8z", fill: false },
                { d: "M12 2.5v2M12 19.5v2M2.5 12h2M19.5 12h2M5.3 5.3l1.4 1.4M17.3 17.3l1.4 1.4M5.3 18.7l1.4-1.4M17.3 6.7l1.4-1.4", fill: false }],
        "power": [{ d: "M12 3v8", fill: false },
                  { d: "M6.6 6.6a7.6 7.6 0 1 0 10.8 0", fill: false }],
        "restart": [{ d: "M4.5 12a7.5 7.5 0 1 0 2.2-5.3", fill: false },
                    { d: "M4.5 3.5v3.7h3.7", fill: false }],
        "suspend": [{ d: "M20 14.6A8.3 8.3 0 0 1 9.4 4a8.3 8.3 0 1 0 10.6 10.6z", fill: false }]
    })

    Repeater {
        model: icon.shapes[icon.name] || []
        Shape {
            required property var modelData
            width: 24
            height: 24
            scale: icon.size / 24
            transformOrigin: Item.TopLeft
            preferredRendererType: Shape.CurveRenderer
            ShapePath {
                strokeColor: icon.color
                strokeWidth: modelData.fill ? 0 : 1.9
                fillColor: modelData.fill ? icon.color : "transparent"
                capStyle: ShapePath.RoundCap
                joinStyle: ShapePath.RoundJoin
                PathSvg { path: modelData.d }
            }
        }
    }
}
