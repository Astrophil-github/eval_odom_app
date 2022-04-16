from PySide2.QtWidgets import QApplication, QMessageBox, QTableWidgetItem, QGraphicsPixmapItem, QGraphicsScene
from PySide2.QtUiTools import QUiLoader
import argparse
from kitti_odometry import KittiEvalOdom
import cv2
from PyQt5.QtGui import QImage, QPixmap

class Stats:

    def __init__(self):
        self.ui = QUiLoader().load('ui/ui.ui')

        self.ui.button.clicked.connect(self.generate)

        self.eval_tool = KittiEvalOdom()


    def generate(self):
        # 获取测试序列
        self.seq = self.ui.comboBox_seq.currentText()
        self.align = self.ui.comboBox_align.currentText()

        # 获取测试序列的file_path和gt_path
        self.file_path = self.ui.lineEdit_test.text()
        self.gt_path = self.ui.lineEdit_gt.text()

        # 如果没有，则是默认
        if self.file_path == '':
            self.file_path = 'test/'
        if self.gt_path == '':
            self.gt_path = 'dataset/kitti_odom/'

        # 获取”是否保存结果文件“的勾选信息，勾选了返回True
        self.if_save = self.ui.radioButton.isChecked()

        self.result = self.eval_tool.eval(
            self.gt_path,
            self.file_path,
            alignment=self.align,
            seq=self.seq,
            if_save=self.if_save,
        )

        # 在列表中打印
        for i in range(len(self.result)):
            self.ui.tableWidget.setItem(
                i-1,
                1,
                QTableWidgetItem("{0:.3f}".format(self.result[i]))
            )

app = QApplication([])
stats = Stats()
stats.ui.show()
app.exec_()