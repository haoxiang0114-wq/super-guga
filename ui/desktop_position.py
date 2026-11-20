"""Keep frameless desktop widgets inside a Qt screen's available work area."""
from PyQt5.QtCore import QPoint, QRect
from PyQt5.QtWidgets import QApplication


def available_work_areas():
    primary = QApplication.primaryScreen()
    screens = QApplication.screens()
    if primary in screens:
        screens.remove(primary)
        screens.insert(0, primary)
    return [screen.availableGeometry() for screen in screens]


def visible_position(window_rect, work_areas):
    areas = [area for area in work_areas if not area.isEmpty()]
    position = window_rect.topLeft()
    if not areas:
        return position

    def overlap(area):
        intersection = area.intersected(window_rect)
        return intersection.width() * intersection.height()

    area = max(areas, key=overlap)
    if not area.intersects(window_rect):
        position = QPoint(
            area.x() + (area.width() - window_rect.width()) // 2,
            area.y() + (area.height() - window_rect.height()) // 2,
        )
    max_x = max(area.left(), area.right() - window_rect.width() + 1)
    max_y = max(area.top(), area.bottom() - window_rect.height() + 1)
    return QPoint(
        max(area.left(), min(position.x(), max_x)),
        max(area.top(), min(position.y(), max_y)),
    )


def move_into_work_area(window, position=None):
    candidate = window.pos() if position is None else position
    target = visible_position(QRect(candidate, window.size()), available_work_areas())
    if target != window.pos():
        window.move(target)

def pet_adjacent_position(pet, size, area):
    """Use the nearest available edge while keeping the pet's face clear."""
    width, height = size.width(), size.height()
    gap = 12
    centered = pet.center().x() - width // 2
    options = (
        QRect(pet.right() + gap, pet.top(), width, height),
        QRect(pet.left() - gap - width, pet.top(), width, height),
        QRect(centered, pet.top() - height - gap, width, height),
        QRect(centered, pet.bottom() + gap, width, height),
    )
    for rect in options:
        if area.contains(rect):
            return rect.topLeft()
    head = pet_face_area(pet)

    def overlap(rect, protected):
        shared = rect.intersected(protected)
        return shared.width() * shared.height()

    max_x = max(area.left(), area.right() - width + 1)
    max_y = max(area.top(), area.bottom() - height + 1)
    clamped = [
        QRect(QPoint(max(area.left(), min(rect.left(), max_x)),
                     max(area.top(), min(rect.top(), max_y))), size)
        for rect in options
    ]
    return min(clamped, key=lambda rect: (overlap(rect, head), overlap(rect, pet))).topLeft()


def pet_face_area(pet):
    return QRect(pet.left(), pet.top(), pet.width(), round(pet.height() * .63))


def keep_face_clear(window, pet):
    if window.isMaximized() or window.isFullScreen():
        return
    geometry = pet.frameGeometry()
    if not window.frameGeometry().intersects(pet_face_area(geometry)):
        return
    screen = QApplication.screenAt(geometry.center()) or QApplication.primaryScreen()
    if screen:
        window.move(pet_adjacent_position(geometry, window.frameGeometry().size(), screen.availableGeometry()))
