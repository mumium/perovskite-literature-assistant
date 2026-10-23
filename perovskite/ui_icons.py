"""Scalable, consistent toolbar icons; no external font or network resources."""
from PySide6.QtCore import QByteArray,Qt
from PySide6.QtGui import QIcon,QImage,QPainter,QPixmap
from PySide6.QtSvg import QSvgRenderer

PATHS={
 'moon':'<path d="M20.5 14A9 9 0 0 1 10 3.5 9 9 0 1 0 20.5 14Z"/>',
 'sun':'<circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1.5 1.5m11 11L19 19M5 19l1.5-1.5m11-11L19 5"/>',
 'highlight':'<path d="m14 3 7 7-9 9-7-7z" fill="#ffd84d"/><path d="m5 12-2 7 7-2" fill="#ffd84d"/><path d="M3 22h18m-9-17 7 7"/>',
 'unhighlight':'<path d="m14 3 7 7-9 9-7-7z" fill="#ffd84d"/><path d="m5 12-2 7 7-2" fill="#ffd84d"/><path d="M3 22h9m0-17 7 7"/><path d="m17 18 5 5m0-5-5 5" stroke="#c53939" stroke-width="2.3"/>',
 'font_color':'<defs><linearGradient id="fontRainbow" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#f05c64"/><stop offset=".25" stop-color="#edb42e"/><stop offset=".5" stop-color="#32b691"/><stop offset=".75" stop-color="#4e86e9"/><stop offset="1" stop-color="#a953cf"/></linearGradient></defs><path d="M10.3 3h3.4l7 18h-4l-1.5-4.3H8.8L7.3 21h-4L10.3 3Zm1.7 5-2.1 5.8h4.2L12 8Z" fill="url(#fontRainbow)" fill-rule="evenodd" stroke="none"/>',
 'import':'<path d="M5 3h9l5 5v13H5zM14 3v6h5M8 14h8m-4-4v8"/>',
 'folder':'<path d="M3 6h7l2 3h9v11H3zM12 11v6m-3-3h6"/>',
 'attach':'<path d="m9 14 7-7a3 3 0 0 1 4 4l-9 9a5 5 0 0 1-7-7l9-9M7 16l9-9"/>',
 'export':'<path d="M3 7 12 3l9 4v13l-9 3-9-3zM3 7l9 4 9-4M12 11v12M8 15h8m-3-3 3 3-3 3"/>',
 'result':'<path d="M5 3h10l4 4v14H5zM8 12l3 3 5-6M14 3v5h5"/>',
 'prompt':'<path d="M8 7H4v14h12v-4M8 3h12v14H8zM11 7h6M11 11h5"/>',
 'api':'<path d="m7 7-5 5 5 5M17 7l5 5-5 5M14 4l-4 16"/>',
 'excel':'<path d="M3 4h18v16H3zM3 9h18M9 4v16M15 9v11"/>',
 'switch':'<path d="M3 5h8l2 3h8v12H3zM6 12h10m-3-3 3 3-3 3M17 17H7m3-3-3 3 3 3"/>',
 'settings':'<path d="M4 6h16M4 12h16M4 18h16"/><circle cx="8" cy="6" r="2" fill="#fff"/><circle cx="16" cy="12" r="2" fill="#fff"/><circle cx="10" cy="18" r="2" fill="#fff"/>',
 'backup':'<path d="M4 3h14l3 3v15H3V3zM7 3v6h10V3M7 21v-8h10v8"/>',
 'restore':'<path d="M4 9a9 9 0 1 1 0 8M4 3v6h6M12 7v6l4 2"/>',
 'history':'<circle cx="12" cy="12" r="9"/><path d="M12 6v6l4 3"/>',
 'tags':'<path d="M3 3h9l9 9-9 9-9-9z"/><circle cx="7" cy="7" r="1.5"/>',
 'help':'<circle cx="12" cy="12" r="9"/><path d="M9 9a3 3 0 0 1 6 0c0 2-3 2-3 5M12 17v1"/>',
}

def svg_pixmap(svg,size=32,density=2):
    raw=svg.encode('utf-8') if isinstance(svg,str) else svg
    renderer=QSvgRenderer(QByteArray(raw))
    image=QImage(round(size*density),round(size*density),QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter=QPainter(image); renderer.render(painter); painter.end()
    image.setDevicePixelRatio(density)
    return QPixmap.fromImage(image)

def icon(name):
    from .theme import is_dark
    stroke='#d1d1d6' if is_dark() else '#48484a'
    svg=f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{stroke}" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">{PATHS[name]}</svg>'
    if is_dark(): svg=svg.replace('fill="#fff"','fill="#2c2c2e"')
    result=QIcon()
    for density in [1,2,3]:
        pixmap=svg_pixmap(svg,24,density); result.addPixmap(pixmap)
        # Keep the requested rainbow A recognizable even before a comment is
        # selected; the surrounding button still indicates its disabled state.
        if name=='font_color': result.addPixmap(pixmap,QIcon.Mode.Disabled)
    return result
