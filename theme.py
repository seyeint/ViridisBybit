"""
Viridis palette, stylesheet and font — shared by the window and the paint
widgets so every colour on the board comes from one place.
"""

BG         = "#111217"
BG_RAISED  = "#16181E"
BG_INPUT   = "#1A1D25"
BORDER     = "#22252E"
BORDER_FCS = "#3A3F4D"
TEXT       = "#C4C8D4"
TEXT_DIM   = "#5C6170"
TEXT_MUTED = "#3D4150"
WHITE      = "#F2F4F8"
ACCENT     = "#6E9FFF"
POSITIVE   = "#3FB68B"
NEGATIVE   = "#D9534F"
AMBER      = "#E8A838"

MONO = "Menlo"                                  # QFont family for painted widgets
MONO_CSS = "'Menlo', 'Consolas', monospace"     # stylesheet / rich-text family

STYLESHEET = f"""
QMainWindow {{ background-color: {BG}; }}
QWidget {{
    color: {TEXT};
    font-family: {MONO_CSS};
    font-size: 12px;
}}
QToolTip {{
    background: {BG_RAISED}; color: {TEXT}; border: 1px solid {BORDER_FCS};
    padding: 4px 6px; font-size: 11px;
}}

/* ── Inputs ──────────────────────────────────── */
QLineEdit {{
    background: {BG_INPUT};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 3px;
    padding: 8px 9px;
    font-size: 13px;
    selection-background-color: {ACCENT};
    selection-color: {BG};
}}
QLineEdit:focus {{ border-color: {ACCENT}; }}
QLineEdit#symbol_input {{
    font-size: 14px;
    font-weight: 700;
    color: {WHITE};
    letter-spacing: 0.5px;
}}
QLineEdit#card_edit {{ padding: 3px 6px; font-size: 11px; border-color: {BORDER_FCS}; }}

/* ── Labels ──────────────────────────────────── */
QLabel {{ background: transparent; border: none; padding: 0; margin: 0; }}
QLabel#dim {{ font-size: 10.5px; color: {TEXT_DIM}; }}
QLabel#muted {{ font-size: 9px; color: {TEXT_MUTED}; letter-spacing: 1.5px; }}
QLabel#preview {{
    color: {TEXT}; font-size: 11px; background: {BG_RAISED};
    border: 1px solid {BORDER_FCS}; border-radius: 3px; padding: 8px 10px;
}}
QLabel#preview[state="error"] {{ color: {NEGATIVE}; border-color: {NEGATIVE}; }}
QLabel#warnings {{ font-size: 10.5px; }}
QLabel#sub {{
    font-size: 10.5px; color: {TEXT_DIM}; padding: 6px 9px;
    border: 1px dashed {BORDER}; border-radius: 3px;
}}
QLabel#placeholder {{
    color: {TEXT_MUTED}; font-size: 11px; font-weight: 600;
    background: {BG_RAISED}; border: 1px solid {BORDER};
    border-radius: 3px; padding: 8px 10px;
}}
QLabel#closed_row {{
    color: {TEXT_MUTED}; font-size: 10.5px;
    border: 1px solid {BORDER}; border-radius: 3px; padding: 6px 12px;
}}

/* ── Checkboxes ──────────────────────────────── */
QCheckBox {{ font-size: 11px; color: {TEXT_DIM}; spacing: 6px; }}
QCheckBox::indicator {{
    width: 12px; height: 12px;
    border: 1px solid {BORDER_FCS};
    border-radius: 2px;
    background: {BG_INPUT};
}}
QCheckBox::indicator:checked {{
    background: {ACCENT};
    border-color: {ACCENT};
}}
QCheckBox:disabled {{ color: {TEXT_MUTED}; }}

/* ── Buttons ─────────────────────────────────── */
QPushButton {{
    font-size: 12px;
    font-weight: 600;
    border-radius: 3px;
    padding: 9px 14px;
    border: 1px solid {BORDER};
    background: {BG_RAISED};
    color: {TEXT_DIM};
}}
QPushButton:hover {{ border-color: {BORDER_FCS}; color: {TEXT}; }}
QPushButton:pressed {{ background: {BG_INPUT}; }}
QPushButton:disabled {{ color: {TEXT_MUTED}; border-color: {BORDER}; }}

/* side and execute buttons get explicit per-widget sheets in main.py:
   QPushButton ignores a background that arrives via a property selector */

QPushButton#chip {{
    font-size: 10px; font-weight: 500; letter-spacing: 0.5px; padding: 3px 8px;
    border: 1px solid {BORDER}; background: transparent; color: {TEXT_DIM};
}}
QPushButton#chip[on="true"] {{ color: {TEXT}; border-color: {BORDER_FCS}; background: {BG_RAISED}; }}

QPushButton#small {{
    font-size: 9px; padding: 3px 8px; color: {TEXT_MUTED};
    border: 1px solid {BORDER}; background: transparent;
}}
QPushButton#small:hover {{ color: {TEXT}; }}

QPushButton#act {{
    font-size: 10px; font-weight: 500; padding: 4px 8px;
    border: 1px solid {BORDER}; background: transparent; color: {TEXT_DIM};
}}
QPushButton#act:hover {{ border-color: {BORDER_FCS}; color: {TEXT}; }}
QPushButton#act[kind="danger"]  {{ border-color: #7A3230; color: {NEGATIVE}; }}
QPushButton#act[kind="primary"] {{ border-color: #3C5A99; color: {ACCENT}; }}

QPushButton#drawer_head {{
    text-align: left; font-size: 9px; letter-spacing: 1.5px; padding: 8px 0;
    border: none; background: transparent; color: {TEXT_MUTED};
}}
QPushButton#drawer_head:hover {{ color: {TEXT_DIM}; }}
QPushButton#margin_btn {{
    font-size: 10px; font-weight: 500; padding: 0; border: none; background: transparent; color: {TEXT_DIM};
}}
QPushButton#margin_btn:hover {{ color: {ACCENT}; }}

/* ── Cards ───────────────────────────────────── */
QFrame#card {{
    background: {BG_RAISED};
    border: 1px solid {BORDER};
    border-left: 3px solid {TEXT_MUTED};
    border-radius: 3px;
}}
QFrame#card[stripe="pos"] {{ border-left-color: {POSITIVE}; }}
QFrame#card[stripe="neg"] {{ border-left-color: {NEGATIVE}; }}
QFrame#card[selected="true"] {{ border-color: {ACCENT}; }}
QFrame#strip {{ background: {BG_RAISED}; border: 1px solid {BORDER}; border-radius: 3px; }}
QFrame#gov {{ border: 1px solid {BORDER}; border-radius: 4px; }}
QFrame#ticket {{ border-right: 1px solid {BORDER}; }}
QFrame#drawer {{ border-top: 1px solid {BORDER}; }}

/* ── Console ─────────────────────────────────── */
QTextEdit#console {{
    background: {BG};
    color: {TEXT_DIM};
    border: none;
    padding: 0 0 6px 0;
    font-size: 10.5px;
}}

/* ── Autocomplete ────────────────────────────── */
QListView {{
    background: {BG_INPUT};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 3px;
    padding: 2px;
    font-size: 12px;
    font-weight: 600;
    outline: none;
}}
QListView::item {{ padding: 5px 8px; border-radius: 2px; }}
QListView::item:hover {{ background: {BG_RAISED}; color: #FFFFFF; }}
QListView::item:selected {{ background: {BORDER}; color: #FFFFFF; }}

/* ── Scrollbar ───────────────────────────────── */
QScrollBar:vertical {{
    background: {BG}; width: 5px; border: none;
}}
QScrollBar::handle:vertical {{
    background: {BORDER}; border-radius: 2px; min-height: 18px;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}

/* ── Message boxes ───────────────────────────── */
QMessageBox {{ background: {BG_RAISED}; }}
QMessageBox QLabel {{ color: {TEXT}; font-size: 12px; }}
"""
