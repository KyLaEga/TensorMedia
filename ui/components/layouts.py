from PySide6.QtWidgets import QLayout, QStyle, QWidget, QSizePolicy
from PySide6.QtCore import Qt, QRect, QSize, QPoint

class FlowLayout(QLayout):
    """Кастомный макет для динамического размещения карточек (Flexbox-like)."""
    def __init__(self, parent=None, margin=-1, hSpacing=-1, vSpacing=-1):
        super().__init__(parent)
        self.m_hSpace = hSpacing
        self.m_vSpace = vSpacing
        self.itemList = []
        self.setContentsMargins(margin, margin, margin, margin)

    def __del__(self):
        item = self.takeAt(0)
        while item:
            item = self.takeAt(0)

    def addItem(self, item):
        self.itemList.append(item)

    def horizontalSpacing(self):
        if self.m_hSpace >= 0: return self.m_hSpace
        return self.smartSpacing(QStyle.PixelMetric.PM_LayoutHorizontalSpacing)

    def verticalSpacing(self):
        if self.m_vSpace >= 0: return self.m_vSpace
        return self.smartSpacing(QStyle.PixelMetric.PM_LayoutVerticalSpacing)

    def count(self):
        return len(self.itemList)

    def itemAt(self, index):
        if 0 <= index < len(self.itemList): return self.itemList[index]
        return None

    def takeAt(self, index):
        if 0 <= index < len(self.itemList): return self.itemList.pop(index)
        return None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self.doLayout(QRect(0, 0, width, 0), True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self.doLayout(rect, False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for item in self.itemList:
            size = size.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        size += QSize(m.left() + m.right(), m.top() + m.bottom())
        return size

    def doLayout(self, rect, testOnly):
        x, y = rect.x(), rect.y()
        lineHeight = 0
        for item in self.itemList:
            spaceX = self.horizontalSpacing()
            spaceY = self.verticalSpacing()
            nextX = x + item.sizeHint().width() + spaceX
            if nextX - spaceX > rect.right() and lineHeight > 0:
                x = rect.x()
                y = y + lineHeight + spaceY
                nextX = x + item.sizeHint().width() + spaceX
                lineHeight = 0
            if not testOnly:
                item.setGeometry(QRect(QPoint(x, y), item.sizeHint()))
            x = nextX
            lineHeight = max(lineHeight, item.sizeHint().height())
        return y + lineHeight - rect.y()

    def smartSpacing(self, pm):
        parent = self.parent()
        if not parent: return -1
        elif parent.isWidgetType(): return parent.style().pixelMetric(pm, None, parent)
        else: return parent.spacing()

class FlowContainer(QWidget):
    """Обертка для проброса динамической высоты FlowLayout в QScrollArea."""
    def __init__(self, parent=None, margin=0, hSpacing=10, vSpacing=10):
        super().__init__(parent)
        self.flow_layout = FlowLayout(self, margin=margin, hSpacing=hSpacing, vSpacing=vSpacing)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.MinimumExpanding)

    def addWidget(self, widget):
        self.flow_layout.addWidget(widget)

    def count(self):
        return self.flow_layout.count()

    def takeAt(self, index):
        return self.flow_layout.takeAt(index)

    def clear(self):
        while self.flow_layout.count():
            item = self.flow_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def sizeHint(self):
        return self.minimumSize()

    def minimumSizeHint(self):
        return QSize(0, self.flow_layout.heightForWidth(self.width()))
        
    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.setMinimumHeight(self.flow_layout.heightForWidth(self.width()))