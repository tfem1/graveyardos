// The installer's window in the live desktop: the local page
// (graveyard-installer) in a Qt WebEngine view, without a browser.
import QtQuick
import QtQuick.Window
import QtWebEngine

Window {
    id: win
    width: 1100
    height: 760
    minimumWidth: 720
    minimumHeight: 520
    visible: true
    color: "#000000"
    title: "Install GraveyardOS"

    WebEngineView {
        anchors.fill: parent
        url: Qt.application.arguments[Qt.application.arguments.length - 1].startsWith("http")
             ? Qt.application.arguments[Qt.application.arguments.length - 1]
             : "http://127.0.0.1:8475/"
        backgroundColor: "#000000"
        settings.localContentCanAccessRemoteUrls: false
        // links to other sites open nowhere: this window is the installer only
        onNewWindowRequested: function (request) { request.reject ? request.reject() : null }
    }
}
