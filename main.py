import sys

from PyQt6.QtWidgets import QApplication

from checker import MainWindow


def main():
    app = QApplication.instance() or QApplication(sys.argv)
    app.setStyle("windowsvista")

    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()