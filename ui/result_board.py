"""A persistent, resizable result bubble with actual local previews and explanation."""
from pathlib import Path
from PyQt5.QtCore import Qt,QUrl,QByteArray,QTimer,QEvent
from PyQt5.QtGui import QPixmap,QPainter,QDesktopServices,QFont
from PyQt5.QtSvg import QSvgRenderer
from PyQt5.QtWidgets import QDialog,QVBoxLayout,QHBoxLayout,QLabel,QPushButton,QListWidget,QSplitter,QTextBrowser,QScrollArea,QWidget,QApplication,QSizePolicy,QStackedWidget,QFrame,QSizeGrip


class LocalText(QTextBrowser):
    def loadResource(self,kind,url):return QByteArray()


class ResultBoard(QDialog):
    def __init__(self,parent=None):
        super().__init__(parent,Qt.Tool|Qt.FramelessWindowHint|Qt.WindowStaysOnTopHint)
        from ui.desktop_fonts import ensure_readable_fonts
        ensure_readable_fonts()
        self.setWindowTitle('咕嘎 · 成果黑板');self.setObjectName('GugaResultBoard')
        self.setFont(QFont('Microsoft YaHei UI',10))
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setMinimumSize(640,460);self._drag=None;self.web=None
        self.resize(960,720);self.files=[];self.open_receipt={};self._preview_pix=None
        self.setStyleSheet('QFrame#ResultBubbleCard{background:#fffdf7;border:1px solid #d9d7e7;border-radius:22px;} QLabel{color:#334155;} '
            'QTextBrowser,QListWidget{background:#ffffff;border:1px solid #e2e8f0;border-radius:10px;padding:10px;}'
            'QPushButton{padding:7px 14px;border:1px solid #cbd5e1;border-radius:10px;background:#eff6ff;}')
        shell=QVBoxLayout(self);shell.setContentsMargins(5,5,5,14)
        card=QFrame();card.setObjectName('ResultBubbleCard');shell.addWidget(card)
        outer=QVBoxLayout(card);outer.setContentsMargins(18,16,18,12)
        head=QHBoxLayout();self.title=QLabel('成果已放到这里');head.addWidget(self.title,1)
        self.title.installEventFilter(self);self.title.setCursor(Qt.SizeAllCursor)
        canvas=QPushButton('自由画布');canvas.clicked.connect(self.show_canvas);head.addWidget(canvas)
        expand=QPushButton('放大');expand.clicked.connect(self.toggle_expanded);head.addWidget(expand)
        close=QPushButton('收起');close.clicked.connect(self.hide);head.addWidget(close);outer.addLayout(head)
        split=QSplitter();self.list=QListWidget();self.list.setMaximumWidth(245);split.addWidget(self.list)
        self.image=QLabel();self.image.setAlignment(Qt.AlignCenter);self.image.setMinimumSize(100,160)
        self.image.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Ignored)
        self.preview_scroll=QScrollArea();self.preview_scroll.setWidgetResizable(True)
        self.preview_scroll.setWidget(self.image);self.stack=QStackedWidget();self.stack.addWidget(self.preview_scroll);split.addWidget(self.stack)
        split.splitterMoved.connect(lambda *args:self._fit_image())
        outer.addWidget(split,3);self.explanation=LocalText();self.explanation.setMinimumHeight(150)
        outer.addWidget(self.explanation,2);self.path=QLabel();self.path.setWordWrap(True)
        self.path.setTextInteractionFlags(Qt.TextSelectableByMouse);outer.addWidget(self.path)
        actions=QHBoxLayout();self.open_button=QPushButton('打开文件');self.folder_button=QPushButton('打开所在文件夹')
        self.open_button.clicked.connect(self.open_selected);self.folder_button.clicked.connect(self.open_folder)
        actions.addWidget(self.open_button);actions.addWidget(self.folder_button);actions.addStretch();outer.addLayout(actions)
        actions.addWidget(QSizeGrip(self))
        self.list.currentRowChanged.connect(self.preview)

    def present(self,goal,files,explanation=''):
        self.files=files;self.title.setText(str(goal or '这次的成果')[:80]);self.list.clear()
        for item in files:self.list.addItem(('✓ ' if item.get('readable') else '待检查 · ')+item['name'])
        self.image.clear();self.path.clear();self._preview_pix=None
        self.list.setVisible(len(files)>1);self.stack.setCurrentWidget(self.preview_scroll)
        if not files:self.image.setText('这一轮的说明在下方。')
        self.set_explanation(explanation)
        if files:self.list.setCurrentRow(0)
        screen=QApplication.screenAt(self.parent().pos()) if self.parent() else QApplication.primaryScreen()
        if screen:
            rect=screen.availableGeometry();self.resize(min(960,rect.width()-40),min(720,rect.height()-60))
            self.move(rect.center()-self.rect().center())
        self.show();self.raise_()
        QTimer.singleShot(0,self._fit_image)

    def resizeEvent(self,event):
        super().resizeEvent(event)
        QTimer.singleShot(0,self._fit_image)

    def _fit_image(self):
        if self._preview_pix is None:return
        size=self.preview_scroll.viewport().size()
        self.image.setPixmap(self._preview_pix.scaled(max(1,size.width()-16),max(1,size.height()-16),
            Qt.KeepAspectRatio,Qt.SmoothTransformation))

    def set_explanation(self,text):self.explanation.setPlainText(str(text))

    def selected(self):
        n=self.list.currentRow();return self.files[n] if 0<=n<len(self.files) else None

    def preview(self,index):
        from core.artifact_review import still_current
        self.image.clear();self._preview_pix=None;item=self.selected()
        if not item:return
        current=still_current(item);self.open_button.setEnabled(current);self.folder_button.setEnabled(True)
        self.path.setText(item['path']);self.image.setWordWrap(True)
        self.stack.setCurrentWidget(self.preview_scroll)
        if not current:
            self.image.setText(item.get('error') or '文件有变化，需要重新检查后打开。');return
        path=Path(item['path']);pix=None
        if item.get('preview_kind')=='html':
            try:
                self._web().load_file(path);self.stack.setCurrentWidget(self.web)
                self.open_button.setText('在黑板中打开');return
            except Exception as exc:self.image.setText('网页暂未展开：'+str(exc));return
        self.open_button.setText('打开文件')
        if item.get('preview_kind')=='pdf':
            import fitz
            with fitz.open(path) as doc:
                frame=doc[0].get_pixmap(matrix=fitz.Matrix(1,1));pix=QPixmap();pix.loadFromData(frame.tobytes('png'))
        elif path.suffix.lower()=='.svg':
            renderer=QSvgRenderer(str(path));size=renderer.defaultSize()
            if size.isEmpty():
                from PyQt5.QtCore import QSize
                size=QSize(1000,650)
            size.scale(1400,1000,Qt.KeepAspectRatio)
            pix=QPixmap(size);pix.fill(Qt.white)
            painter=QPainter(pix);renderer.render(painter);painter.end()
        elif item.get('preview_kind')=='image':pix=QPixmap(str(path))
        if pix is not None and not pix.isNull():
            self._preview_pix=pix;self._fit_image()
        else:self.image.setText(item.get('excerpt','')[:4500] or '\n'.join(item.get('checks',[])))

    def open_selected(self):
        from core.artifact_review import still_current
        item=self.selected()
        if not item or not still_current(item):return False
        if item.get('preview_kind')=='html':
            self.preview(self.list.currentRow())
            self.open_receipt=dict(path=item['path'],open_requested=self.stack.currentWidget() is self.web,
                render_verified=bool(self.web and self.web.loaded),embedded=True,external_browser_launched=False)
            return self.open_receipt['open_requested']
        opened=QDesktopServices.openUrl(QUrl.fromLocalFile(item['path']))
        self.open_receipt=dict(path=item['path'],open_requested=bool(opened),render_verified=False)
        return bool(opened)

    def open_folder(self):
        item=self.selected()
        if item:QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(item['path']).parent)))

    def _web(self):
        if self.web is None:
            from ui.local_web_view import LocalWebView
            self.web=LocalWebView(self);self.stack.addWidget(self.web)
        return self.web

    def show_canvas(self):
        from ui.local_web_view import FREE_CANVAS
        self._web().load_inline(FREE_CANVAS);self.stack.setCurrentWidget(self.web)
        self.path.setText('自由画布 · 可以直接画；当前成果文件仍保留在列表中')

    def toggle_expanded(self):
        if self.isMaximized():self.showNormal()
        else:self.showMaximized()

    def eventFilter(self,obj,event):
        if obj is self.title:
            if event.type()==QEvent.MouseButtonPress and event.button()==Qt.LeftButton:self._drag=event.globalPos()-self.pos();return True
            if event.type()==QEvent.MouseMove and self._drag is not None:self.move(event.globalPos()-self._drag);return True
            if event.type()==QEvent.MouseButtonRelease:self._drag=None;return True
        return super().eventFilter(obj,event)

    def diagnostics(self,capture=False):
        result=dict(visible=self.isVisible(),open_receipt=self.open_receipt,
            web=self.web.state() if self.web and self.stack.currentWidget() is self.web else None)
        if self.web and self.web.loaded and self.open_receipt.get('embedded'):self.open_receipt['render_verified']=True
        if capture:
            path=Path(__file__).resolve().parents[1]/'runtime/browser-bubble-upgrade-20261114/live-board.png'
            path.parent.mkdir(parents=True,exist_ok=True)
            if self.grab().save(str(path)):result['capture_path']=str(path)
        return result
