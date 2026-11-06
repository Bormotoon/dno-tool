"""Предметные SVG-миниатюры оборудования со светом сверху слева.

Реальные материалы и детали дополняют узнаваемый силуэт устройства.
Все рисунки помещаются в viewBox 0 0 128 128 и имеют прозрачный фон.
"""

from collections.abc import Callable


class Drawing:
    """Построитель SVG с общей палитрой материалов."""

    def __init__(self, mode: str):
        self.parts: list[str] = []
        self.edge = "#344453" if mode == "paper" else "#627383"
        self.flat = mode == "line"
        self.definitions = '''<defs>
          <linearGradient id="shell" x1="0" y1="0" x2="0.7" y2="1">
            <stop stop-color="#FFFFFF"/><stop offset="0.5" stop-color="#E9EDF0"/><stop offset="1" stop-color="#AAB8C4"/>
          </linearGradient>
          <linearGradient id="metal" x1="0" y1="0" x2="0" y2="1">
            <stop stop-color="#D9E2E9"/><stop offset="0.45" stop-color="#AABBC9"/><stop offset="1" stop-color="#778F9F"/>
          </linearGradient>
          <linearGradient id="graphite" x1="0" y1="0" x2="0.8" y2="1">
            <stop stop-color="#596774"/><stop offset="0.55" stop-color="#303D49"/><stop offset="1" stop-color="#18242F"/>
          </linearGradient>
          <linearGradient id="side" x1="0" y1="0" x2="1" y2="1">
            <stop stop-color="#8295A4"/><stop offset="1" stop-color="#485D6D"/>
          </linearGradient>
          <linearGradient id="screen" x1="0" y1="0" x2="1" y2="1">
            <stop stop-color="#488DB3"/><stop offset="0.5" stop-color="#22506E"/><stop offset="1" stop-color="#0A233B"/>
          </linearGradient>
          <linearGradient id="glass" x1="0" y1="0" x2="0.8" y2="1">
            <stop stop-color="#667F92"/><stop offset="0.45" stop-color="#243D50"/><stop offset="1" stop-color="#0B1829"/>
          </linearGradient>
          <radialGradient id="lens" cx="0.38" cy="0.3" r="0.7">
            <stop stop-color="#88EDFF"/><stop offset="0.25" stop-color="#2699BA"/><stop offset="0.5" stop-color="#124869"/><stop offset="1" stop-color="#071425"/>
          </radialGradient>
        </defs>'''

    def material(self, fill: str) -> str:
        colors = {"shell": "#E7EDF2", "metal": "#AABBC9", "graphite": "#354552",
                  "side": "#617789", "screen": "#285D7C", "glass": "#203D53", "lens": "#157C9C"}
        if fill in colors:
            return colors[fill] if self.flat else f"url(#{fill})"
        return fill

    def path(self, d: str, fill: str = "none", stroke: str | None = None,
             width: float = 1.5, opacity: float = 1) -> None:
        stroke = self.edge if stroke is None else stroke
        self.parts.append(f'<path d="{d}" fill="{self.material(fill)}" stroke="{stroke}" '
                          f'stroke-width="{width}" stroke-linejoin="round" stroke-linecap="round" opacity="{opacity}"/>')

    def rect(self, x, y, w, h, fill, r=2, stroke=None, width=1.5) -> None:
        stroke = self.edge if stroke is None else stroke
        self.parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{r}" '
                          f'fill="{self.material(fill)}" stroke="{stroke}" stroke-width="{width}"/>')

    def ellipse(self, x, y, rx, ry, fill, stroke=None, width=1.5, opacity=1) -> None:
        stroke = self.edge if stroke is None else stroke
        self.parts.append(f'<ellipse cx="{x}" cy="{y}" rx="{rx}" ry="{ry}" '
                          f'fill="{self.material(fill)}" stroke="{stroke}" stroke-width="{width}" opacity="{opacity}"/>')

    def dot(self, x, y, r=1.7, color="#7FE6A0") -> None:
        self.ellipse(x, y, r, r, color, "none")

    def shadow(self, x=64, y=112, rx=46, ry=5) -> None:
        self.ellipse(x, y, rx, ry, "#061323", "none", opacity=0.16)

    def port(self, x, y, w=9, h=9) -> None:
        """Гнездо RJ45 с верхним вырезом и контактами."""
        self.rect(x, y, w, h, "#111F2B", 1, "#91A5B5", 0.8)
        self.path(f"M{x+2} {y+1} H{x+w-2} V{y+3} H{x+w-1} V{y+h-1} H{x+1} V{y+3} H{x+2} Z",
                  "#263847", "none")
        for i in range(3):
            xx = x + 2.5 + i * (w - 5) / 2
            self.path(f"M{xx} {y+h-3} V{y+h-1.5}", stroke="#D7B675", width=0.65)

    def svg(self) -> str:
        return self.definitions + "".join(self.parts)


def router(s: Drawing) -> None:
    s.shadow(y=106, rx=53)
    for d in ("M26 68 L18 25 Q17 21 20 20 Q23 19 24 24 L34 64 Z",
              "M60 62 V15 Q60 11 64 11 Q68 11 68 15 V62 Z",
              "M94 64 L104 24 Q105 20 108 21 Q111 22 110 26 L103 70 Z"):
        s.path(d, "graphite", "#8393A1")
    s.path("M12 75 L29 57 Q31 54 37 54 H98 Q103 54 106 58 L117 77 L108 92 H20 Z", "shell")
    s.path("M12 75 Q60 85 117 77 L113 96 Q64 107 16 95 Z", "graphite")
    s.path("M19 74 Q63 82 110 76", stroke="#FFFFFF", width=2)
    for x in range(42, 86, 7):
        s.path(f"M{x} 68 L{x+4} 61", stroke="#8C9CA9", width=1.5)
    for x in (35, 43, 51):
        s.dot(x, 91, 1.7)
    s.path("M80 91 H98", stroke="#7D909E", width=2)


def access_point(s: Drawing) -> None:
    s.shadow(y=102, rx=44, ry=6)
    s.ellipse(64, 74, 47, 26, "side")
    s.path("M17 63 V74 C18 108 109 107 111 74 V63 Z", "metal")
    s.ellipse(64, 63, 47, 29, "shell", width=1.8)
    s.ellipse(64, 61, 35, 20, "shell", "#B5C3CE", 1)
    s.path("M35 55 C44 41 80 38 94 54", stroke="#FFFFFF", width=3)
    s.ellipse(64, 64, 9, 5, "#DCE5EB", "#B6C4CF", 0.8)
    s.path("M56 83 Q64 86 72 83", stroke="#4FCBDF", width=2.8)


def switch(s: Drawing) -> None:
    s.shadow(y=99, rx=55)
    s.path("M8 60 L26 40 H111 L121 57 L106 66 Z", "shell")
    s.path("M8 60 H108 V91 H8 Z", "metal", width=1.8)
    s.path("M108 60 L121 57 V84 L108 91 Z", "side")
    s.path("M12 63 H104", stroke="#EDF4F9", width=1.2)
    s.rect(12, 67, 8, 15, "graphite", 1)
    s.dot(16, 71, 1.4)
    s.dot(16, 77, 1.4, "#EDC972")
    for i in range(8):
        s.port(24 + i * 9.6, 69, 8, 12)
        if i % 2 == 0:
            s.dot(28 + i * 9.6, 85, 1)
    s.path("M35 50 H90 M40 46 H85", stroke="#BAC7D1", width=1.2)
    for x in (12, 103):
        s.dot(x, 87, 1.1, "#526B7E")


def printer(s: Drawing) -> None:
    s.shadow(y=115, rx=44)
    s.path("M30 33 L44 19 H102 L113 34 L97 49 Z", "shell")
    s.path("M30 33 H98 V47 H30 Z", "graphite")
    s.path("M19 48 L30 37 H99 L114 52 L101 70 Z", "shell")
    s.path("M19 48 H101 V104 Q101 110 95 110 H25 Q19 110 19 104 Z", "shell", width=1.8)
    s.path("M101 48 L114 52 V99 L101 110 Z", "side")
    s.rect(29, 63, 61, 15, "graphite", 3)
    s.path("M39 72 H81 L87 101 H32 Z", "#FFFFFF", "#BBC9D2", 1)
    s.path("M42 82 H76 M40 88 H77 M39 94 H68", stroke="#A5B5C1", width=1.4)
    s.rect(76, 50, 17, 9, "screen", 1.5, "#526F84", 1)
    s.dot(67, 55, 1.7, "#4EA8CF")
    s.path("M29 103 H91", stroke="#899EAE", width=1.6)
    s.path("M37 28 H96", stroke="#FFFFFF", width=2)


def camera(s: Drawing) -> None:
    s.shadow(y=113, rx=32)
    s.path("M23 48 Q23 36 36 33 H92 Q105 36 105 48 V57 Q64 78 23 57 Z", "shell", width=1.8)
    s.ellipse(64, 47, 41, 17, "shell")
    s.path("M28 56 C27 87 39 107 64 107 C89 107 101 86 100 56 Q64 74 28 56 Z", "glass", "#8DA2B2", 1.8)
    s.ellipse(64, 65, 31, 10, "graphite", "#5B7489", 1)
    s.ellipse(64, 84, 19, 19, "#162532", "#5A7184", 2)
    s.ellipse(64, 84, 12, 12, "lens", "#0C1925", 2)
    s.ellipse(60, 80, 3.2, 2.2, "#A2E8F4", "none", opacity=0.65)
    s.path("M35 66 Q34 79 42 89", stroke="#DBEDF5", width=2.5, opacity=0.55)
    s.dot(85, 84, 1.4, "#B7D5E2")


def nas(s: Drawing) -> None:
    s.shadow(y=113, rx=35)
    s.path("M26 26 L42 15 H100 L87 29 Z", "metal")
    s.path("M87 26 L100 15 V98 L87 109 Z", "side")
    s.rect(26, 26, 61, 83, "graphite", 4, "#8FA1AF", 1.8)
    for x in (33, 57):
        s.rect(x, 34, 21, 62, "#1C2934", 2, "#657887", 1)
        s.path(f"M{x+3} 41 H{x+18} M{x+3} 45 H{x+18}", stroke="#526574", width=1)
        s.rect(x+7, 70, 7, 18, "graphite", 2, "#718493", 1)
        s.dot(x+10.5, 54, 1.5)
    s.dot(78, 102, 2, "#5EC5E5")
    s.path("M34 103 H65", stroke="#516776", width=2)
    for y in (45, 51, 57, 63, 69, 75):
        s.path(f"M92 {y} L96 {y-3}", stroke="#3F5363", width=1)


def phone(s: Drawing) -> None:
    s.shadow(y=113, rx=41)
    s.path("M36 29 L96 29 L111 99 Q112 106 105 108 H34 Q27 107 28 101 Z", "side", width=1.8)
    s.path("M37 24 H94 L105 98 H31 Z", "metal", "#8195A5", 1.8)
    s.path("M58 33 H89 L92 54 H57 Z", "#21394B", "#5B7285", 1.5)
    s.path("M61 37 H85 L87 48 H60 Z", "#95BFC5", "none")
    s.path("M64 40 H81 M64 44 H75", stroke="#50767C", width=1)
    for row in range(4):
        for col in range(3):
            x = 60 + col * 11 + row * 0.8
            y = 64 + row * 7.3
            s.rect(x, y, 8, 4.8, "graphite", 1, "#8499A9", 0.7)
    s.path("M34 22 Q41 17 49 23 L52 37 Q52 41 47 43 L43 43 Q38 59 42 76 L47 77 Q52 78 52 83 L50 97 Q44 103 35 99 Q21 67 27 38 Q28 28 34 22 Z", "graphite", "#91A3B0", 1.8)
    s.path("M35 28 Q30 37 31 49 M35 79 L38 94", stroke="#7D8F9E", width=2)
    s.path("M47 98 Q42 112 52 112 Q59 112 57 104", stroke="#344A5C", width=2)


def monitor(s: Drawing, x=13, y=26, w=94, h=63) -> None:
    s.path(f"M{x+w*0.43} {y+h-1} L{x+w*0.42} {y+h+15} H{x+w*0.64} L{x+w*0.6} {y+h-1} Z", "metal")
    s.path(f"M{x+w*0.3} {y+h+14} L{x+w*0.68} {y+h+14} L{x+w*0.8} {y+h+20} H{x+w*0.18} Z", "shell")
    s.rect(x,y,w,h,"graphite",4,"#95A7B5",1.7)
    s.rect(x+5,y+5,w-10,h-13,"screen",1,"#142A3B",1)
    s.path(f"M{x+7} {y+7} H{x+w-8} L{x+7} {y+h-18} Z", "#91D0E8", "none", opacity=0.1)
    s.dot(x+w/2,y+h-4,1.1,"#A5B6C2")


def desktop(s: Drawing) -> None:
    s.shadow(y=114,rx=55)
    s.path("M88 24 L101 15 L119 22 L107 31 Z", "metal")
    s.path("M107 29 L119 22 V101 L107 109 Z", "side")
    s.rect(86,24,21,85,"graphite",3,"#90A4B3",1.6)
    s.path("M90 36 H103 M90 41 H103",stroke="#9BAEBB",width=1.4)
    s.dot(97,53,2.2,"#70D3F3")
    s.path("M91 83 V98 M95 83 V98 M99 83 V98 M103 83 V98",stroke="#607584",width=1)
    monitor(s,8,34,81,56)
    s.path("M39 51 L51 49 V61 H39 Z M54 49 L68 47 V61 H54 Z M39 64 H51 V76 L39 74 Z M54 64 H68 V78 L54 76 Z", "#A8DCF4", "none")


def host(s: Drawing) -> None:
    s.shadow(y=114,rx=49)
    monitor(s,15,22,98,66)
    s.path("M22 104 H91 L103 115 H11 Z", "metal", "#849AAA",1)
    for y,x0,x1 in ((107,23,88),(110,20,92)):
        s.path(f"M{x0} {y} H{x1}",stroke="#DCE6EE",width=1.3)
    s.path("M40 113 H72",stroke="#DCE6EE",width=1.5)
    s.path("M22 72 L36 60 L49 66 L66 45 L82 54 L107 35",stroke="#71BDD9",width=1.5,opacity=0.55)


def linux(s: Drawing) -> None:
    s.shadow(y=112,rx=55)
    s.rect(21,20,87,66,"graphite",4,"#A0B0BD",1.8)
    s.rect(27,26,75,53,"screen",1,"#132A3A",1)
    s.dot(64,23,0.8,"#C3CED6")
    s.path("M21 86 H108 L121 106 Q121 110 115 111 H13 Q7 110 8 106 Z", "shell", "#7B91A1",1.8)
    s.path("M29 89 H101 L108 100 H22 Z","#556B7C","#9BAEBC",0.8)
    for y in (92,95,98):
        s.path(f"M29 {y} H100",stroke="#ABBDC9",width=0.8)
    s.path("M51 102 H78 L81 107 H48 Z","metal","#9CAEBB",0.8)
    s.rect(34,34,60,37,"#112331",3,"#7297AC",0.8)
    s.path("M35 42 H93",stroke="#506E80",width=1)
    for x,color in ((40,"#E69B8D"),(45,"#DBCD83"),(50,"#8ACBA0")):
        s.dot(x,38,1.1,color)
    s.path("M43 50 L50 56 L43 62 M56 63 H68",stroke="#99DBBE",width=2.8)


def virtual_machine(s: Drawing) -> None:
    s.shadow(y=112,rx=43)
    s.path("M39 18 H109 V72 H39 Z","metal","#9CAEBB",1.6)
    s.path("M44 23 H104 V60 H44 Z","screen","#314E63",1)
    s.path("M20 43 H90 V103 H20 Z","shell","#8EA3B2",1.8)
    s.rect(26,49,58,42,"screen",2,"#3B596E",1)
    s.path("M55 53 L72 63 L55 73 L38 63 Z","#96C9E1","#D3EAF5",1)
    s.path("M38 63 L55 73 V87 L38 77 Z","#477C9B","#B9DBEA",1)
    s.path("M55 73 L72 63 V77 L55 87 Z","#245775","#B9DBEA",1)
    s.path("M45 97 H66",stroke="#8B9EAC",width=2)
    s.dot(79,97,1.8,"#6DDBAF")
    s.path("M96 85 H110 V98 H101",stroke="#9EBBD0",width=2)
    s.path("M105 93 L100 98 L105 103",stroke="#9EBBD0",width=2)


def web_device(s: Drawing) -> None:
    s.shadow(y=110,rx=48)
    s.rect(13,23,102,82,"shell",6,"#8FA5B4",1.8)
    s.path("M18 28 H110 V40 H18 Z","metal","none")
    s.rect(21,46,51,49,"screen",2,"#3F5C70",1)
    for x,col in ((25,"#CB8583"),(33,"#D6BB76"),(41,"#8FBEA1")):
        s.dot(x,34,2,col)
    s.rect(54,30,49,8,"#EDF4F7",3,"#A5B7C4",0.8)
    s.path("M29 57 H60 M29 64 H54 M29 80 H43 M29 86 H54",stroke="#8BB7C9",width=2)
    s.ellipse(88,76,28,28,"shell","#607F94",1.8)
    s.ellipse(88,76,23,23,"screen","#A9C4D3",1)
    s.ellipse(88,76,11,23,"none","#A5D1E1",1.5)
    s.path("M66 76 H110 M69 65 Q88 72 107 65 M69 87 Q88 80 107 87",stroke="#A5D1E1",width=1.5)


def network_device(s: Drawing) -> None:
    s.shadow(y=114,rx=35)
    s.path("M32 30 L47 18 H94 L80 30 Z","shell")
    s.path("M80 30 L94 18 V102 L80 113 Z","side")
    s.rect(32,30,48,83,"shell",4,"#8EA4B3",1.8)
    s.rect(38,39,36,65,"graphite",3,"#8C9FAA",1)
    s.dot(46,48,2,"#61CFAD")
    s.dot(55,48,2,"#70CBEA")
    s.port(46,60,19,14)
    s.port(46,81,19,14)
    s.path("M37 113 H77",stroke="#485F71",width=3)


def unknown(s: Drawing) -> None:
    s.shadow(y=110,rx=40)
    s.path("M22 52 L46 31 H101 L108 45 L83 64 Z","shell")
    s.path("M22 52 H83 V100 H22 Z","metal","#748B9E",1.8)
    s.path("M83 52 L108 45 V89 L83 100 Z","side")
    s.path("M43 65 Q44 55 55 56 Q67 57 66 67 Q66 73 56 77 V82",stroke="#344F65",width=5)
    s.dot(56,90,2.5,"#344F65")
    s.path("M91 64 L100 61 M91 69 L100 66 M91 74 L100 71",stroke="#A8B9C6",width=1.2)


def local(s: Drawing) -> None:
    host(s)
    s.ellipse(100,96,23,23,"shell","#547991",1.8)
    s.ellipse(100,89,6,7,"graphite","none")
    s.path("M87 110 Q87 98 100 98 Q113 98 113 110 Z","graphite","none")


def network(s: Drawing) -> None:
    s.path("M64 46 V70 M22 70 H106 M22 70 V85 M106 70 V85 M64 70 V89",stroke="#74A7C6",width=4)
    s.path("M39 17 H88 L97 25 L86 32 H31 Z","shell")
    s.rect(31,25,57,25,"metal",3,"#849CAC",1.5)
    for x in (39,50,61,72):
        s.port(x,32,8,10)
    for x in (8,49,90):
        s.rect(x,84,30,21,"graphite",2,"#A0B3C1",1.3)
        s.rect(x+3,87,24,13,"screen",1,"none",0)
        s.path(f"M{x+15} 106 V111 M{x+8} 112 H{x+22}",stroke="#9AAEBD",width=2.5)


def projector(s: Drawing) -> None:
    s.shadow(y=105, rx=49)
    s.path("M14 57 L32 35 H101 L116 53 L100 69 Z", "shell")
    s.path("M14 57 H101 V96 H19 Q14 96 14 90 Z", "shell", width=1.8)
    s.path("M101 57 L116 53 V87 L101 96 Z", "side")
    s.ellipse(74, 76, 21, 21, "graphite", "#8BA0B0", 2)
    s.ellipse(74, 76, 15, 15, "lens", "#122B3D", 2)
    s.ellipse(69, 71, 4, 3, "#B4F0FF", "none", opacity=.7)
    for y in (68, 74, 80, 86):
        s.path(f"M23 {y} H43", stroke="#738C9E", width=2)
    s.dot(39, 51, 2)
    s.path("M50 45 H79 M24 98 V101 M91 98 V101", stroke="#7C91A1", width=3)


def television(s: Drawing) -> None:
    s.shadow(y=111, rx=52)
    s.path("M35 90 L25 110 H34 L47 90 M81 90 L94 110 H103 L93 90", "metal")
    s.rect(9, 21, 110, 74, "graphite", 4, "#91A5B5", 1.8)
    s.rect(14, 26, 100, 62, "screen", 1, "#152D3E", 1)
    s.path("M15 76 L41 47 L62 67 L81 39 L113 78 V87 H15 Z", "#427F9A", "none")
    s.path("M15 27 H112 L15 66 Z", "#B2DAE9", "none", opacity=.13)
    s.dot(64, 92, 1, "#8DD7DC")


def media_player(s: Drawing) -> None:
    s.shadow(y=105, rx=49)
    s.path("M13 68 L29 47 H82 L96 66 L79 81 Z", "graphite", "#8297A8", 1.8)
    s.path("M13 68 H80 V96 H19 Q13 96 13 90 Z", "graphite", "#8297A8", 1.8)
    s.path("M80 68 L96 66 V87 L80 96 Z", "side")
    s.path("M39 56 L39 69 L53 62 Z", "#A3DDF2", "none")
    s.dot(28, 85, 2, "#70DDB7")
    s.rect(44, 81, 23, 6, "#122330", 1, "#7D93A4", .8)
    s.path("M99 25 Q106 20 113 25 L122 89 Q122 95 116 97 L109 98 Q104 98 103 92 L94 32 Q93 27 99 25 Z", "graphite", "#A4B4BF", 1.6)
    s.ellipse(105, 44, 7, 8, "metal", "#9AB0C0", 1)
    s.dot(105, 44, 2.4, "#344F64")
    for y in (64, 75, 86):
        s.path(f"M106 {y} L113 {y-1}", stroke="#9AB0C0", width=2.5)


def network_audio(s: Drawing) -> None:
    s.shadow(y=115, rx=31)
    s.path("M34 26 L49 16 H91 L81 28 Z", "metal")
    s.path("M81 26 L91 16 V103 L81 113 Z", "side")
    s.rect(34, 26, 47, 87, "graphite", 7, "#91A4B2", 1.8)
    s.ellipse(57, 47, 10, 10, "#172A39", "#8499A9", 1.5)
    s.ellipse(57, 47, 4, 4, "metal", "#5E7C91", 1)
    s.ellipse(57, 83, 18, 19, "#182A38", "#8CA3B3", 1.8)
    s.ellipse(57, 83, 12, 13, "graphite", "#506D81", 1.5)
    s.ellipse(57, 83, 6, 6, "metal", "#6E879A", 1)
    s.dot(57, 106, 1.4, "#6EDCCC")


def mobile_screen(s: Drawing, x, y, w, h) -> None:
    """Стекло мобильного устройства с общей палитрой экранов."""
    s.rect(x, y, w, h, "graphite", 8, "#9AAEBE", 1.8)
    s.rect(x+4, y+7, w-8, h-14, "screen", 4, "#152E42", .8)
    s.path(f"M{x+5} {y+9} H{x+w-6} L{x+5} {y+h*.62} Z", "#A3DBEB", "none", opacity=.14)
    s.path(f"M{x+w*.37} {y+h-11} H{x+w*.63}", stroke="#B6D9E6", width=2)


def smartphone(s: Drawing) -> None:
    s.shadow(y=116, rx=25)
    mobile_screen(s, 39, 12, 50, 99)
    s.rect(54, 19, 20, 4, "#172735", 2, "none", 0)
    s.path("M49 77 Q62 54 78 48 M49 87 Q62 66 78 61", stroke="#64A7C4", width=2)
    s.path("M36 36 V47 M92 40 V55", stroke="#879DAC", width=2)


def tablet(s: Drawing) -> None:
    s.shadow(y=116, rx=43)
    mobile_screen(s, 22, 13, 84, 98)
    s.dot(64, 17, 1, "#1E3447")
    s.path("M35 77 L51 59 L66 68 L92 40", stroke="#72B4CF", width=2)
    s.path("M114 37 L114 96 L111 104 L108 96 V37 Z", "shell", "#7D93A5", 1)


def mac(s: Drawing) -> None:
    s.shadow(y=114, rx=48)
    s.path("M57 88 L54 104 H80 L74 88 Z", "metal")
    s.path("M45 105 H85 L94 112 H36 Z", "shell")
    s.rect(16, 18, 96, 77, "shell", 4, "#93A5B4", 1.8)
    s.rect(20, 22, 88, 59, "screen", 1, "#253E50", 1)
    s.path("M21 68 Q46 32 65 59 Q86 83 107 37 V80 H21 Z", "#487F9F", "none")
    s.path("M21 23 H107 L21 62 Z", "#B5DFEB", "none", opacity=.12)
    s.dot(64, 88, 2, "#8197A9")
    s.dot(64, 20, .8, "#344D61")


def server(s: Drawing) -> None:
    s.shadow(y=114, rx=45)
    s.path("M19 29 L33 17 H110 L98 31 Z", "metal")
    s.path("M98 29 L110 17 V101 L98 111 Z", "side")
    s.rect(19, 29, 79, 82, "graphite", 3, "#92A6B5", 1.8)
    for y in (36, 60, 84):
        s.rect(24, y, 69, 21, "metal", 2, "#5B7386", 1)
        s.rect(29, y+4, 39, 13, "#223545", 1, "#889EAE", .8)
        for x in (35, 44, 53, 62):
            s.path(f"M{x} {y+6} V{y+14}", stroke="#6C8495", width=1)
        s.dot(76, y+7, 1.6)
        s.dot(84, y+7, 1.4, "#69CBE4")
        s.rect(74, y+12, 13, 4, "#253B4B", 1, "none", 0)


def thin_client(s: Drawing) -> None:
    s.shadow(y=115, rx=48)
    monitor(s, 7, 25, 81, 59)
    s.path("M93 36 L103 27 L116 31 L106 40 Z", "shell")
    s.path("M106 39 L116 31 V102 L106 110 Z", "side")
    s.rect(91, 36, 15, 74, "graphite", 3, "#8FA4B4", 1.5)
    s.dot(98.5, 49, 2, "#71D9C0")
    for y in (66, 78):
        s.rect(96, y, 5, 8, "#142B3C", 1, "#ACC0CC", .8)
    s.path("M86 112 H112 M39 47 L49 56 L39 65 M57 65 H67", stroke="#84AFC4", width=2)


def ups(s: Drawing) -> None:
    s.shadow(y=115, rx=33)
    s.path("M31 27 L45 16 H96 L84 30 Z", "shell")
    s.path("M84 28 L96 16 V102 L84 112 Z", "side")
    s.rect(31, 27, 53, 85, "graphite", 5, "#92A8B8", 1.8)
    s.rect(41, 38, 33, 22, "screen", 2, "#97B6C8", 1)
    s.path("M58 41 L49 51 H57 L54 58 L66 47 H58 Z", "#92E9D5", "none")
    s.ellipse(57, 72, 4, 4, "metal", "#829CAF", 1)
    for y in (87, 93, 99, 105):
        s.path(f"M40 {y} H75", stroke="#6B8396", width=1.8)


def pdu(s: Drawing) -> None:
    s.shadow(y=116, rx=20)
    s.path("M46 14 H79 V114 H46 Z", "metal", "#8A9FAF", 1.6)
    s.rect(41, 20, 44, 88, "graphite", 3, "#94A9B8", 1.8)
    s.rect(49, 27, 28, 12, "screen", 1, "#7C9AAF", 1)
    s.path("M54 31 H71 M54 35 H65", stroke="#81DCC9", width=1.5)
    for y in (46, 66, 86):
        s.rect(49, y, 28, 15, "#1A2C3A", 2, "#9AACB9", 1)
        s.path(f"M55 {y+5} V{y+10} M71 {y+5} V{y+10} M63 {y+3} V{y+7}", stroke="#B4C3CC", width=1.8)
    s.dot(63, 16, 1.5, "#425E73")
    s.dot(63, 111, 1.5, "#425E73")


def access_control(s: Drawing) -> None:
    s.shadow(y=115, rx=40)
    s.path("M29 23 L40 15 H88 L80 25 Z", "metal")
    s.path("M80 23 L88 15 V102 L80 110 Z", "side")
    s.rect(29, 23, 51, 87, "graphite", 5, "#91A7B7", 1.8)
    s.rect(36, 33, 36, 19, "screen", 2, "#64879F", 1)
    s.path("M45 42 H63 M45 47 H56", stroke="#7FD6D3", width=1.5)
    s.path("M47 63 Q59 55 68 65 M50 68 Q59 61 65 68 M53 73 Q59 68 62 73", stroke="#A7C9D9", width=2)
    s.rect(57, 79, 42, 30, "shell", 3, "#91A6B5", 1.3)
    s.rect(62, 85, 10, 9, "metal", 1, "#879EAF", .8)
    s.path("M77 89 H92 M64 101 H90", stroke="#7E9DAF", width=1.8)


def intercom(s: Drawing) -> None:
    s.shadow(y=115, rx=28)
    s.path("M39 21 L49 13 H88 L81 23 Z", "shell")
    s.path("M81 21 L88 13 V103 L81 111 Z", "side")
    s.rect(39, 21, 42, 90, "metal", 4, "#91A7B7", 1.8)
    s.rect(44, 27, 32, 31, "graphite", 3, "#648093", 1)
    s.ellipse(60, 42, 10, 10, "lens", "#0E2638", 1.5)
    s.dot(57, 39, 2, "#9EE8F5")
    for y in (65, 70, 75):
        s.path(f"M49 {y} H71", stroke="#526F84", width=1.6)
    s.ellipse(60, 93, 8, 8, "shell", "#537D96", 1.5)
    s.ellipse(60, 93, 4, 4, "#69BFD3", "none")


def plc(s: Drawing) -> None:
    s.shadow(y=113, rx=49)
    s.path("M12 99 H117 V107 H12 Z", "metal", "#7890A3", 1)
    for x, w in ((16, 44), (63, 24), (90, 24)):
        s.path(f"M{x} 34 L{x+8} 25 H{x+w+6} L{x+w} 36 Z", "shell")
        s.path(f"M{x+w} 34 L{x+w+6} 25 V94 L{x+w} 101 Z", "side")
        s.rect(x, 34, w, 67, "shell", 2, "#8DA3B4", 1.5)
        s.rect(x+4, 39, w-8, 10, "#447B84", 1, "#6E9D9F", .8)
        s.rect(x+4, 85, w-8, 10, "#447B84", 1, "#6E9D9F", .8)
        for xx in range(x+7, x+w-3, 7):
            s.dot(xx, 44, 1.2, "#C8D8DB")
            s.dot(xx, 90, 1.2, "#C8D8DB")
    s.rect(22, 54, 32, 25, "graphite", 2, "#849DAF", 1)
    s.dot(28, 60, 1.5)
    s.dot(35, 60, 1.5, "#E3C77F")
    s.port(29, 66, 17, 10)
    for x in (70, 97):
        for y in (58, 66, 74):
            s.dot(x, y, 1.3)
            s.dot(x+9, y, 1.3, "#6B879A")


def smart_home(s: Drawing) -> None:
    s.shadow(y=112, rx=39)
    s.path("M27 48 L40 35 H98 L101 46 L88 59 Z", "shell")
    s.path("M88 48 L101 46 V95 L88 107 Z", "side")
    s.rect(27, 48, 61, 59, "shell", 5, "#8CA4B5", 1.8)
    s.path("M38 77 L57 60 L77 77 M44 72 V94 H70 V72", stroke="#487A95", width=3)
    s.rect(53, 81, 9, 13, "screen", 1, "none", 0)
    s.dot(78, 99, 1.6)
    s.path("M45 25 Q63 13 81 25 M51 31 Q63 23 75 31", stroke="#71AAC4", width=2.5)
    s.dot(63, 37, 2, "#71AAC4")


def printer_3d(s: Drawing) -> None:
    s.shadow(y=116, rx=46)
    s.path("M29 100 L42 90 H108 L97 104 Z", "metal")
    s.rect(24, 103, 79, 9, "graphite", 2, "#829DAF", 1.2)
    s.path("M27 101 V25 H101 V101 M34 98 V32 H94 V98", stroke="#829BAC", width=5)
    s.rect(20, 44, 88, 7, "metal", 1, "#6A859A", 1)
    s.path("M61 30 V58", stroke="#718DA1", width=2)
    s.rect(52, 46, 23, 17, "graphite", 2, "#9CB2C1", 1.2)
    s.path("M59 63 H68 L64 71 Z", "#D3AC70", "#8A795F", .8)
    s.path("M47 83 L64 73 L81 83 L64 93 Z", "#A4D9E2", "#6B9CAC", 1)
    s.path("M47 83 L64 93 V104 L47 94 Z", "#579DAE", "#6B9CAC", 1)
    s.path("M64 93 L81 83 V94 L64 104 Z", "#327A94", "#6B9CAC", 1)
    s.rect(83, 105, 15, 5, "screen", 1, "none", 0)


def interactive_panel(s: Drawing) -> None:
    s.shadow(y=116, rx=44)
    s.path("M45 86 L40 112 M82 86 L88 112 M32 114 H50 M79 114 H97", stroke="#8CA5B6", width=4)
    s.rect(9, 19, 110, 70, "graphite", 3, "#99AEBD", 1.8)
    s.rect(15, 25, 98, 56, "#D5E5E9", 1, "#5B8097", 1)
    s.path("M26 39 H52 M26 46 H44 M27 69 L38 56 L51 66 L67 42", stroke="#578A9F", width=2.5)
    s.ellipse(87, 49, 12, 12, "screen", "none")
    s.path("M72 72 H103", stroke="#81A5B5", width=2)
    s.path("M38 90 H88", stroke="#94A9B8", width=3)
    s.path("M104 74 L111 48", stroke="#7EADC1", width=3)


DRAWERS: dict[str, Callable[[Drawing], None]] = {
    "router": router, "ap": access_point, "switch": switch, "printer": printer,
    "camera": camera, "nas": nas, "phone": phone, "pc": desktop,
    "host": host, "linux": linux, "vm": virtual_machine, "web": web_device,
    "netdev": network_device, "unknown": unknown, "local": local, "network": network,
    "projector": projector, "tv": television, "media_player": media_player,
    "network_audio": network_audio, "smartphone": smartphone, "tablet": tablet,
    "mac": mac, "server": server, "thin_client": thin_client, "ups": ups,
    "pdu": pdu, "access_control": access_control, "intercom": intercom,
    "plc": plc, "smart_home": smart_home, "3d_printer": printer_3d,
    "interactive_panel": interactive_panel,
}


def draw_hardware(name: str, mode: str) -> str:
    """Назначение иконки задаёт геометрия оборудования, а не цвет."""
    drawing = Drawing(mode)
    DRAWERS[name](drawing)
    return drawing.svg()
