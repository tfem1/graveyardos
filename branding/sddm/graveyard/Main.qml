import QtQuick

// GraveyardOS login screen: the Graveyard Dunes (night or day), a large
// clock with hours over minutes, the date, then the password, who signs in
// and which desktop; light/dark and power at the bottom right. Black and
// white like the rest of GraveyardOS. SDDM 0.21+, Qt 6.
Rectangle {
    id: root

    property date now: new Date()
    property bool dark: config.mode !== "light"
    property bool busy: false
    property string error: ""
    property int userIndex: userModel.lastIndex >= 0 ? userModel.lastIndex : 0
    property int sessionIndex: sessionModel.lastIndex >= 0 ? sessionModel.lastIndex : 0
    readonly property real u: Math.max(0.6, Math.min(2.0, height / 1080))

    // the palette of each mode: GraveyardOS black and white
    readonly property color ink: dark ? "#FFFFFF" : "#0A0A0A"
    readonly property color inkSoft: dark ? "#A3A3A3" : "#5C5C5C"
    readonly property color glass: dark ? Qt.rgba(0, 0, 0, 0.42) : Qt.rgba(1, 1, 1, 0.62)
    readonly property color glassHover: dark ? Qt.rgba(0, 0, 0, 0.62) : Qt.rgba(1, 1, 1, 0.85)
    readonly property color edge: dark ? Qt.rgba(1, 1, 1, 0.14) : Qt.rgba(0, 0, 0, 0.10)
    readonly property color danger: dark ? "#FF8A8A" : "#C62828"

    readonly property string userName: users.count > 0 && users.itemAt(userIndex) ? users.itemAt(userIndex).login : ""
    readonly property string userShown: users.count > 0 && users.itemAt(userIndex) ? users.itemAt(userIndex).shown : ""
    readonly property string sessionName: sessions.count > 0 && sessions.itemAt(sessionIndex) ? sessions.itemAt(sessionIndex).label : ""

    function login() {
        if (!userName || busy)
            return;
        busy = true;
        error = "";
        sddm.login(userName, password.text, sessionIndex);
    }

    width: 1920
    height: 1080
    color: "#000000"
    Component.onCompleted: password.forceActiveFocus()

    FontLoader { id: clockFont; source: "fonts/Rubik-800.ttf" }
    FontLoader { id: textFont; source: "fonts/Rubik-500.ttf" }

    Timer {
        interval: 1000
        running: true
        repeat: true
        onTriggered: root.now = new Date()
    }

    Connections {
        target: sddm
        function onLoginFailed() {
            root.busy = false;
            root.error = "Wrong password";
            password.text = "";
            shake.restart();
            password.forceActiveFocus();
        }
        function onLoginSucceeded() {
            root.busy = false;
        }
    }

    // the users and sessions, read from SDDM's models
    Repeater {
        id: users
        model: userModel
        Item {
            required property string name
            required property string realName
            readonly property string login: name
            readonly property string shown: realName || name
        }
    }
    Repeater {
        id: sessions
        model: sessionModel
        Item {
            required property string name
            readonly property string label: name
        }
    }

    // --- the Dunes: night in dark mode, day in light mode ---------------------
    Image {
        anchors.fill: parent
        source: config.backgroundDay ? Qt.resolvedUrl(config.backgroundDay) : ""
        fillMode: Image.PreserveAspectCrop
        asynchronous: true
    }
    Image {
        anchors.fill: parent
        source: config.backgroundNight ? Qt.resolvedUrl(config.backgroundNight) : ""
        fillMode: Image.PreserveAspectCrop
        asynchronous: true
        opacity: root.dark ? 1 : 0
        Behavior on opacity { NumberAnimation { duration: 450; easing.type: Easing.InOutQuad } }
    }

    // --- the clock: hours over minutes, the digits in two tones, crossed -------
    Column {
        id: clock
        anchors.horizontalCenter: parent.horizontalCenter
        y: parent.height * 0.13
        spacing: -78 * root.u

        Repeater {
            model: [Qt.formatTime(root.now, "HH"), Qt.formatTime(root.now, "mm")]
            Row {
                id: line
                required property string modelData
                required property int index
                anchors.horizontalCenter: parent.horizontalCenter
                spacing: -6 * root.u
                Repeater {
                    model: line.modelData.split("")
                    Text {
                        required property string modelData
                        required property int index
                        text: modelData
                        // top-left and bottom-right bright, the other two soft
                        color: (index + line.index) % 2 === 0 ? root.ink : root.inkSoft
                        font.family: clockFont.font.family
                        font.weight: Font.ExtraBold
                        font.pixelSize: 196 * root.u
                        Behavior on color { ColorAnimation { duration: 450 } }
                    }
                }
            }
        }
    }

    Pill {
        id: date
        anchors.horizontalCenter: parent.horizontalCenter
        anchors.top: clock.bottom
        anchors.topMargin: -14 * root.u
        text: root.now.toLocaleDateString(Qt.locale(), "dddd, MMM d")
        interactive: false
    }

    // --- the password, with who and which desktop under it ----------------------
    Item {
        id: form
        width: 480 * root.u
        height: field.height
        anchors.horizontalCenter: parent.horizontalCenter
        y: parent.height * 0.69

        Rectangle {
            id: field
            width: parent.width
            height: 46 * root.u
            radius: 13 * root.u
            color: root.glass
            border.width: password.activeFocus ? 1.5 : 1
            border.color: root.error ? root.danger : password.activeFocus ? Qt.alpha(root.ink, 0.55) : root.edge
            Behavior on color { ColorAnimation { duration: 450 } }

            Rectangle {
                id: lockBox
                anchors.left: parent.left
                anchors.leftMargin: 10 * root.u
                anchors.verticalCenter: parent.verticalCenter
                width: 26 * root.u
                height: 26 * root.u
                radius: 7 * root.u
                color: Qt.alpha(root.ink, 0.12)
                Icon {
                    anchors.centerIn: parent
                    name: "lock"
                    color: root.ink
                    size: 14 * root.u
                }
            }

            TextInput {
                id: password
                objectName: "password"
                anchors.left: lockBox.right
                anchors.leftMargin: 14 * root.u
                anchors.right: go.left
                anchors.rightMargin: 12 * root.u
                anchors.verticalCenter: parent.verticalCenter
                echoMode: TextInput.Password
                passwordCharacter: "●"
                color: root.ink
                selectionColor: root.ink
                selectedTextColor: root.dark ? "#000000" : "#FFFFFF"
                font.family: textFont.font.family
                font.pixelSize: 18 * root.u
                clip: true
                focus: true
                onAccepted: root.login()
                onTextChanged: if (text) root.error = ""
                Keys.onEscapePressed: text = ""
            }
            Text {
                anchors.fill: password
                visible: !password.text
                verticalAlignment: Text.AlignVCenter
                text: root.error ? root.error
                    : (typeof keyboard !== "undefined" && keyboard && keyboard.capsLock) ? "Password · Caps Lock is on"
                    : "Password"
                color: root.error ? root.danger : Qt.alpha(root.ink, 0.55)
                font.family: textFont.font.family
                font.pixelSize: 17 * root.u
                elide: Text.ElideRight
            }

            Rectangle {
                id: go
                anchors.right: parent.right
                anchors.rightMargin: 10 * root.u
                anchors.verticalCenter: parent.verticalCenter
                width: 26 * root.u
                height: 26 * root.u
                radius: 7 * root.u
                color: root.ink
                opacity: root.busy ? 0.5 : goHover.hovered ? 0.85 : 1
                Behavior on color { ColorAnimation { duration: 450 } }
                Icon {
                    anchors.centerIn: parent
                    name: "next"
                    color: root.dark ? "#000000" : "#FFFFFF"
                    size: 15 * root.u
                }
                HoverHandler { id: goHover; cursorShape: Qt.PointingHandCursor }
                TapHandler { onTapped: root.login() }
            }
        }

        SequentialAnimation {
            id: shake
            NumberAnimation { target: field; property: "x"; to: -14; duration: 50 }
            NumberAnimation { target: field; property: "x"; to: 14; duration: 70 }
            NumberAnimation { target: field; property: "x"; to: -7; duration: 60 }
            NumberAnimation { target: field; property: "x"; to: 0; duration: 50 }
        }
    }

    Row {
        anchors.horizontalCenter: parent.horizontalCenter
        anchors.top: form.bottom
        anchors.topMargin: 14 * root.u
        spacing: 10 * root.u

        Pill {
            icon: "user"
            text: root.userShown
            visible: users.count > 0
            interactive: users.count > 1
            onClicked: { root.userIndex = (root.userIndex + 1) % users.count; password.forceActiveFocus(); }
        }
        Pill {
            icon: "session"
            text: root.sessionName
            visible: sessions.count > 0
            interactive: sessions.count > 1
            onClicked: { root.sessionIndex = (root.sessionIndex + 1) % sessions.count; password.forceActiveFocus(); }
        }
    }

    // --- light/dark and power, bottom right -------------------------------------
    Row {
        anchors.right: parent.right
        anchors.rightMargin: 28 * root.u
        anchors.bottom: parent.bottom
        anchors.bottomMargin: 26 * root.u
        spacing: 10 * root.u

        SquareButton {
            icon: root.dark ? "sun" : "moon"
            hint: root.dark ? "Light" : "Dark"
            onClicked: { root.dark = !root.dark; password.forceActiveFocus(); }
        }
        SquareButton {
            id: powerButton
            icon: "power"
            hint: "Power"
            onClicked: powerMenu.visible = !powerMenu.visible
        }
    }

    // the power menu, above its button
    Rectangle {
        id: powerMenu
        visible: false
        anchors.right: parent.right
        anchors.rightMargin: 28 * root.u
        anchors.bottom: parent.bottom
        anchors.bottomMargin: (26 + 40 + 10) * root.u
        width: menu.width + 12 * root.u
        height: menu.height + 12 * root.u
        radius: 14 * root.u
        color: root.glassHover
        border.width: 1
        border.color: root.edge

        Column {
            id: menu
            anchors.centerIn: parent
            spacing: 2 * root.u
            Repeater {
                model: [
                    { label: "Suspend", icon: "suspend", can: sddm.canSuspend, act: () => sddm.suspend() },
                    { label: "Restart", icon: "restart", can: sddm.canReboot, act: () => sddm.reboot() },
                    { label: "Shut down", icon: "power", can: sddm.canPowerOff, act: () => sddm.powerOff() }
                ]
                Rectangle {
                    required property var modelData
                    visible: modelData.can
                    width: 170 * root.u
                    height: 40 * root.u
                    radius: 10 * root.u
                    color: itemHover.hovered ? Qt.alpha(root.ink, 0.12) : "transparent"
                    Row {
                        anchors.left: parent.left
                        anchors.leftMargin: 12 * root.u
                        anchors.verticalCenter: parent.verticalCenter
                        spacing: 10 * root.u
                        Icon { name: modelData.icon; color: root.ink; size: 18 * root.u; anchors.verticalCenter: parent.verticalCenter }
                        Text {
                            anchors.verticalCenter: parent.verticalCenter
                            text: modelData.label
                            color: root.ink
                            font.family: textFont.font.family
                            font.pixelSize: 15 * root.u
                        }
                    }
                    HoverHandler { id: itemHover; cursorShape: Qt.PointingHandCursor }
                    TapHandler { onTapped: modelData.act() }
                }
            }
        }
    }

    // a click anywhere else closes the power menu and gives the password the keys
    TapHandler {
        onTapped: { powerMenu.visible = false; password.forceActiveFocus(); }
    }

    // --- the small parts ---------------------------------------------------------
    component Pill: Rectangle {
        id: pill
        property string icon: ""
        property string text: ""
        property bool interactive: true
        signal clicked

        width: row.width + 26 * root.u
        height: 32 * root.u
        radius: 10 * root.u
        color: pillHover.hovered && interactive ? root.glassHover : root.glass
        border.width: 1
        border.color: root.edge
        Behavior on color { ColorAnimation { duration: 200 } }

        Row {
            id: row
            anchors.centerIn: parent
            spacing: 7 * root.u
            Icon {
                visible: pill.icon !== ""
                anchors.verticalCenter: parent.verticalCenter
                name: pill.icon
                color: root.ink
                size: 16 * root.u
            }
            Text {
                anchors.verticalCenter: parent.verticalCenter
                text: pill.text
                color: root.ink
                font.family: textFont.font.family
                font.pixelSize: 14 * root.u
            }
        }
        HoverHandler { id: pillHover; cursorShape: pill.interactive ? Qt.PointingHandCursor : Qt.ArrowCursor }
        TapHandler { enabled: pill.interactive; onTapped: pill.clicked() }
    }

    component SquareButton: Rectangle {
        id: square
        property string icon: ""
        property string hint: ""
        signal clicked

        width: 40 * root.u
        height: 40 * root.u
        radius: 11 * root.u
        color: squareHover.hovered ? root.glassHover : root.glass
        border.width: 1
        border.color: root.edge
        Behavior on color { ColorAnimation { duration: 200 } }

        Icon {
            anchors.centerIn: parent
            name: square.icon
            color: root.ink
            size: 20 * root.u
        }
        HoverHandler { id: squareHover; cursorShape: Qt.PointingHandCursor }
        TapHandler { onTapped: square.clicked() }
    }
}
