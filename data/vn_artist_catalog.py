"""Authoritative catalog of Vietnamese artists.
Keyed by normalized_key (ASCII-stripped lowercase).

Covers:
- Indochine School (École des Beaux-Arts de l'Indochine, 1925-1945)
- Mid-century masters (born 1900s-1920s)
- Wartime & reunification artists (born 1930s-1950s)
- Đổi Mới era (born 1950s-1970s)
- Contemporary (born 1970s+)
- French painters permanently associated with Indochina (kept for market relevance)

death_year = None if living.

NON_VN_KNOWN: artists from other SEA countries that get mixed in via Bonhams dept crawl;
these should be filtered OUT.
"""

# Vietnamese artists we want to track
VN_ARTIST_CATALOG = {
    # ===== INDOCHINE SCHOOL PIONEERS (1890s-1910s) =====
    "nam son":                 ("Nam Sơn",                   1890, 1973),
    "nguyen phan chanh":       ("Nguyễn Phan Chánh",         1892, 1984),
    "le van de":               ("Lê Văn Đệ",                 1906, 1966),
    "to ngoc van":             ("Tô Ngọc Vân",               1906, 1954),
    "mai trung thu":           ("Mai Trung Thứ",             1906, 1980),
    "mai thu":                 ("Mai Trung Thứ",             1906, 1980),
    "le pho":                  ("Lê Phổ",                    1907, 2001),
    "le pho ha dong":          ("Lê Phổ",                    1907, 2001),  # Lê Phổ sinh tại Hà Đông
    "vu cao dam":              ("Vũ Cao Đàm",                1908, 2000),
    "cao dam vu":              ("Vũ Cao Đàm",                1908, 2000),
    "dam vu":                  ("Vũ Cao Đàm",                1908, 2000),
    "nguyen gia tri":          ("Nguyễn Gia Trí",            1908, 1993),
    "tran van can":            ("Trần Văn Cẩn",              1910, 1994),
    "le thi luu":              ("Lê Thị Lựu",                1911, 1988),
    "le thy":                  ("Lê Thy",                    1919, 1961),
    "nguyen khang":            ("Nguyễn Khang",              1912, 1989),
    "hoang tich chu":          ("Hoàng Tích Chù",            1912, 2003),
    "nguyen do cung":          ("Nguyễn Đỗ Cung",            1912, 1977),
    "nguyen tien chung":       ("Nguyễn Tiến Chung",         1914, 1976),
    "luong xuan nhi":          ("Lương Xuân Nhị",            1914, 2006),
    "nguyen van ty":           ("Nguyễn Văn Tỵ",             1917, 1992),
    "pham van don":            ("Phạm Văn Đôn",              1917, 2000),
    "ta thuc binh":            ("Tạ Thúc Bình",              1917, 1998),
    "le van xuong":            ("Lê Văn Xương",              1917, 1988),
    "luong xuan qua":          ("Lương Xuân Quá",            1917, 1988),
    "nguyen tu nghiem":        ("Nguyễn Tư Nghiêm",          1918, 2016),
    "le quoc loc":             ("Lê Quốc Lộc",               1918, 1987),
    "pham hau":                ("Phạm Hậu",                  1903, 1995),
    "tran quang tran":         ("Trần Quang Trân",           1900, 1969),
    "nguyen s":                ("Nguyễn Sáng",               1923, 1988),  # partial match
    "mai van hien":            ("Mai Văn Hiến",              1923, 2006),
    "luu van sin":             ("Lưu Văn Sìn",               1923, 2001),

    # ===== MID-CENTURY MASTERS (1920s-1930s) =====
    "bui xuan phai":           ("Bùi Xuân Phái",             1920, 1988),
    "tran duy":                ("Trần Duy",                  1920, 2014),
    "le ba dang":              ("Lê Bá Đảng",                1921, 2015),
    "lebadang":                ("Lê Bá Đảng",                1921, 2015),
    "lebadang le ba dang":     ("Lê Bá Đảng",                1921, 2015),
    "diem phung thi":          ("Điềm Phùng Thị",            1920, 2002),
    "phung thi":               ("Điềm Phùng Thị",            1920, 2002),
    "diem phung-thi":          ("Điềm Phùng Thị",            1920, 2002),
    "phung thi diem":          ("Điềm Phùng Thị",            1920, 2002),
    "diem phungthi":           ("Điềm Phùng Thị",            1920, 2002),
    "huynh van thuan":         ("Huỳnh Văn Thuận",           1921, 2017),
    "nguyen sang":             ("Nguyễn Sáng",               1923, 1988),
    "duong bich lien":         ("Dương Bích Liên",           1924, 1988),
    "nguyen trong hop":        ("Nguyễn Trọng Hợp",          1925, 1999),
    "huynh phuong dong":       ("Huỳnh Phương Đông",         1925, 2015),
    "pham cung":               ("Phạm Cung",                 1926, 2018),
    "tran luu hau":            ("Trần Lưu Hậu",              1928, 2020),
    "luu cong nhan":           ("Lưu Công Nhân",             1929, 2007),
    "le thiet cuong":          ("Lê Thiết Cương",            1962, None),
    "pham kim binh":           ("Phạm Kim Bình",             1930, 2014),

    # ===== POST-WAR / KHÁNG CHIẾN (1930s-1940s) =====
    "nguyen trung":            ("Nguyễn Trung",              1940, None),
    "trung nguyen":            ("Nguyễn Trung",              1940, None),  # Western-order alias (Ravenel etc.)
    "ho huu thu":              ("Hồ Hữu Thủ",                1940, 2024),
    "nguyen thi hiep":         ("Nguyễn Thị Hiệp",           1935, None),
    "do xuan doan":            ("Đỗ Xuân Doãn",              1937, 2015),
    "do duy tuan":             ("Đỗ Duy Tuấn",               1950, None),
    "nguyen dinh dung":        ("Nguyễn Đình Dũng",          1943, None),
    "dang phuong viet":        ("Đặng Phương Việt",          1962, None),
    "dang xuan hoa":           ("Đặng Xuân Hòa",             1959, None),  # ensure in catalog
    "nguyen duc nung":         ("Nguyễn Đức Nùng",           1932, 2003),
    "le nang hien":            ("Lê Năng Hiển",              1921, 2014),
    "trinh cung":              ("Trịnh Cung",                1939, None),
    "dinh cuong":              ("Đinh Cường",                1939, 2016),
    "nguyen quan":             ("Nguyễn Quân",               1938, None),
    "nguyen hoang hoanh":      ("Nguyễn Hoàng Hoanh",        1937, 2025),
    "do quang em":             ("Đỗ Quang Em",               1942, 2021),
    "nguyen nhan":             ("Nguyễn Nhân",               1943, None),
    "pham luc":                ("Phạm Lực",                  1943, None),
    "do son":                  ("Đỗ Sơn",                    1943, None),
    "luu cong toan":           ("Lưu Công Toàn",             1944, None),
    "dinh y nhi":              ("Đinh Ý Nhi",                1967, None),
    "nguyen thanh chuong":     ("Nguyễn Thanh Chương",       1949, None),
    "thanh chuong":            ("Thành Chương",              1949, None),

    # ===== ĐỔI MỚI ERA (1950s-1960s) =====
    "ca le thang":             ("Ca Lê Thắng",               1949, None),
    "ly truc son":             ("Lý Trực Sơn",               1949, None),
    "dao hai phong":           ("Đào Hải Phong",             1965, None),
    "le thanh son":            ("Lê Thanh Sơn",              1961, None),
    "le huy tiep":             ("Lê Huy Tiếp",               1951, None),
    "dang xuan hoa":           ("Đặng Xuân Hòa",             1959, None),
    "nguyen thi tam":          ("Nguyễn Thị Tâm",            1955, None),
    "nguyen quan huy":         ("Nguyễn Quân Huy",           1954, None),
    "nguyen thanh binh":       ("Nguyễn Thanh Bình",         1954, None),
    "thanh binh nguyen":       ("Nguyễn Thanh Bình",         1954, None),  # Western-order alias (Ravenel)
    "boi tran":                ("Bội Trân",                  1957, None),  # Huế painter
    "le chanh":                ("Lê Chánh",                  1940, None),  # TP.HCM
    "luong khanh toan":        ("Lương Khánh Toàn",          1955, None),  # Ninh Bình
    "nguyen dieu thuy":        ("Nguyễn Diệu Thủy",          1962, None),  # TP.HCM
    "pham luan":               ("Phạm Luận",                 1954, None),
    "bui huu hung":            ("Bùi Hữu Hùng",              1957, None),
    "tran luong":              ("Trần Lương",                1960, None),
    "tran van thao":           ("Trần Văn Thảo",             1961, None),
    "nguyen thai tuan":        ("Nguyễn Thái Tuấn",          1965, 2023),
    "hong viet dung":          ("Hồng Việt Dũng",            1962, None),
    "dinh quan":               ("Đinh Quân",                 1964, None),
    "ha tri hieu":             ("Hà Trí Hiếu",               1959, None),
    "tran khanh chuong":       ("Trần Khánh Chương",         1943, 2020),
    "luu xuan nhi":            ("Lưu Xuân Nhi",              1956, None),
    "tran luu viet":           ("Trần Lưu Việt",             1956, None),
    "nguyen van cuong":        ("Nguyễn Văn Cường",          1972, None),
    "pham an hai":             ("Phạm An Hải",               1967, None),
    "hoang phuong vy":         ("Hoàng Phượng Vỹ",           1962, None),

    # ===== CONTEMPORARY (1970s-1990s+) =====
    "bui cong khanh":          ("Bùi Công Khánh",            1972, None),
    "bui tien tuan":           ("Bùi Tiến Tuấn",             1971, None),
    "le quang ha":             ("Lê Quảng Hà",               1963, None),
    "nguyen manh hung":        ("Nguyễn Mạnh Hùng",          1976, None),
    "nguyen huy an":           ("Nguyễn Huy An",             1982, None),
    "tran thao mien":          ("Trần Thảo Miên",            1987, None),
    "bui thanh tam":           ("Bùi Thanh Tâm",             1979, None),
    "tran the vinh":           ("Trần Thế Vĩnh",             1979, None),
    "khong do duy":            ("Khổng Đỗ Duy",              1985, None),
    "doan quynh nhu":          ("Đoàn Quỳnh Như",            None, None),
    "dinh q le":               ("Đinh Q. Lê",                1968, 2024),
    "pham huy thong":          ("Phạm Huy Thông",            1981, None),
    "nguyen tan cuong":        ("Nguyễn Tấn Cương",          1953, None),
    "ha manh thang":           ("Hà Mạnh Thắng",             1980, None),
    "nguyen cong hoai":        ("Nguyễn Công Hoài",          1974, None),
    "lam manh hien":           ("Lâm Mạnh Hiền",             1977, None),
    "nguyen thai binh":        ("Nguyễn Thái Bình",          None, None),
    "le kinh tai":             ("Lê Kinh Tài",               1967, None),
    "nguyen the son":          ("Nguyễn Thế Sơn",            1978, None),
    "tran nhat thang":         ("Trần Nhật Thăng",           1972, None),

    # ===== CONTEMPORARY MULTIMEDIA / VIDEO / INSTALLATION / PERFORMANCE =====
    # Reserved here for when video/installation/performance lots get crawled.
    "tuan andrew nguyen":      ("Tuấn Andrew Nguyễn",        1976, None),
    "tuan-andrew nguyen":      ("Tuấn Andrew Nguyễn",        1976, None),
    "nguyen tuan andrew":      ("Tuấn Andrew Nguyễn",        1976, None),
    "le brothers":             ("Lê Brothers",               1975, None),    # twins Lê Ngọc Thanh + Lê Đức Hải, b.1975
    "le ngoc thanh le duc hai": ("Lê Brothers",              1975, None),
    "le quy anh hao":          ("Lê Quý Anh Hào",            None, None),

    # ===== DIASPORA / French-Vietnamese =====
    "pierre le-tan":           ("Pierre Lê-Tân",             1950, 2019),
    "pierre le tan":           ("Pierre Lê-Tân",             1950, 2019),
    "jean volang":             ("Jean Volang",               1921, 2009),
    "vu dinh khoi":            ("Vũ Đình Khôi",              None, None),
    "nam thai":                ("Nam Thái",                  -20, None),  # -20 = Thế kỷ 20
    "duc loi":                 ("Atelier Duc-Loi",           None, None),

    # ===== FRENCH PAINTERS OF INDOCHINA (market-relevant) =====
    "alix ayme":               ("Alix Aymé",                 1894, 1989),
    "alix dailhac ayme":       ("Alix Aymé",                 1894, 1989),
    "alix hava":               ("Alix Aymé",                 1894, 1989),
    "joseph inguimberty":      ("Joseph Inguimberty",        1896, 1971),
    "evariste jonchere":       ("Évariste Jonchère",         1892, 1956),
    "victor tardieu":          ("Victor Tardieu",            1870, 1937),
    "andre maire":             ("André Maire",               1898, 1984),
    "jean despujols":          ("Jean Despujols",            1886, 1965),

    # ===== DB contemporary artists from our gallery imports =====
    # Noah Bùi = real name Bùi Văn Hoàn (b. 1981)
    "noah bui":                ("Noah Bùi (Bùi Văn Hoàn)",   1981, None),
    "bui van hoan":            ("Noah Bùi (Bùi Văn Hoàn)",   1981, None),
    "nguyen thi thu ha":       ("Nguyễn Thị Thu Hà",         1979, None),
    "tran minh kim thoai":     ("Trần Minh Kim Thoại",       None, None),
    "dao minh tuan":           ("Đào Minh Tuấn",             2003, None),
    "dao minh tu":             ("Đào Minh Tú",               2003, None),  # silk specialist
    "vu tuan viet":            ("Vũ Tuấn Việt",              1992, None),
    "duong thuy duong":        ("Dương Thuỳ Dương",          1979, None),
    "duong thuy duong 1":      ("Dương Thuỳ Dương",          1979, None),
    "nguyen ngoc liem":        ("Nguyễn Ngọc Liêm",          1989, None),
    "pham duy hoang":          ("Phạm Duy Hoàng",            1963, None),
    "nguyen viet thanh":       ("Nguyễn Viết Thanh",         1964, None),
    "tran trung linh":         ("Trần Trung Lĩnh",           1977, None),
    "bui van tuat":            ("Bùi Văn Tuất",              1982, None),
    "bui hoang duong":         ("Bùi Hoàng Dương",           1981, None),
    "le minh khoa":            ("Lê Minh Khoa",              1974, None),
    "nguyen trung hieu":       ("Nguyễn Trung Hiếu",         1974, None),
    "nguyen thanh quoc thanh": ("Nguyễn Thành Quốc Thạnh",   1953, None),
    "pham thanh toan":         ("Phạm Thanh Toàn",           1991, None),
    "vu binh minh":            ("Vũ Bình Minh",              1985, None),
    "tran hanh":               ("Hạnh Trần",                 1996, None),
    "hanh tran":               ("Hạnh Trần",                 1996, None),
    "bui chat":                ("Bùi Chát",                  1979, None),
    "phan anh thu":            ("Phan Anh Thư",              2000, None),
    "tran ngoc linh":          ("Trần Ngọc Linh",            1987, None),
    "khong do duy":            ("Khổng Đỗ Duy",              1987, None),
    "nguyen cong hoai":        ("Nguyễn Công Hoài",          1984, None),
    "doan quynh nhu":          ("Đoàn Quỳnh Như",            1980, None),

    # ===== Additional VN artists discovered from auction data =====
    "phan cam thuong":         ("Phan Cẩm Thượng",           1957, None),
    "tran trong vu":           ("Trần Trọng Vũ",             1964, None),
    "truong tan":              ("Trương Tân",                1963, None),
    "nguyen xuan tiep":        ("Nguyễn Xuân Tiệp",          1956, None),
    "tran phuc duyen":         ("Trần Phúc Duyên",           1923, 1993),
    "van duong thanh":         ("Văn Dương Thành",           1951, None),
    "nguyen van giao":         ("Nguyễn Văn Giao",           1933, None),
    "henri nguyen quy kien":   ("Henri Nguyễn Quý Kiển",     None, None),
    "nguyen than":             ("Nguyễn Thân",               1948, None),
    "nguyen thu":              ("Nguyễn Thụ",                1930, None),
    "nguyen anh":              ("Nguyễn Anh",                1914, 2000),
    "nguyen cuong":            ("Nguyễn Cường",              None, None),
    "le vinh":                 ("Lê Vinh",                   1923, None),
    "le huy toan":             ("Lê Huy Toàn",               1930, 2015),
    "hoang duc dung":          ("Hoàng Đức Dũng (Hoang Duc Dzung)", 1971, None),
    "hoang duc dzung":         ("Hoàng Đức Dũng (Hoang Duc Dzung)", 1971, None),
    "nguyen quang bao":        ("Nguyễn Quang Bảo",          1929, None),
    "truong hanh":             ("Trương Hạnh",               -20, None),  # -20 = "Thế kỷ 20" marker
    "nguyen van bang":         ("Nguyễn Văn Bằng",           1958, None),
    "nguyen tran canh":        ("Nguyễn Trân Cảnh",          1980, None),
    "nguyen mai thu":          ("Nguyễn Mai Thu",            -20, None),  # 20th-century painter, distinct from Mai Trung Thứ
    "quoc thai":               ("Quốc Thái",                 1943, 2020),  # Hải Phòng
    "quan le":                 ("Lê Quân",                   1953, None),
    "le quan":                 ("Lê Quân",                   1953, None),

    # ===== Other observed artists (need year research) =====
    "hoang sung":              ("Hoàng Sùng",                1932, 2002),
    "nguyen hue":              ("Nguyễn Huệ",                1940, None),
    "nguyen the khang":        ("Nguyễn Thế Khang",          1932, None),
    "pham van lien":           ("Phạm Văn Liên",             1923, None),
    "to ngoc thanh":           ("Tô Ngọc Thành",             1942, None),
    "bui van hoan":            ("Bùi Văn Hoan",              1940, None),
    "nguyen tri minh":         ("Nguyễn Trí Minh",           1924, 2004),
    "nguyen thanh long":       ("Nguyễn Thanh Long",         1936, None),
    "nguyen thanh":            ("Nguyen Thanh",              -20, None),  # XXe siècle lacquer artist (Millon vente2124, 2019); kept latinised since identity not confirmed
    "quynh huong":             ("Quỳnh Hương",               1958, None),
    "tran nguyen dung":        ("Trần Nguyên Dũng",          1956, None),
    "nguyen huyen":            ("Nguyễn Huyến",              1915, 1990),
    "nguyen van binh":         ("Nguyễn Văn Bình",           1917, 2004),
    "le vuong":                ("Lê Vượng",                  1918, 2021),
    "thanh van":               ("Thanh Vân",                 1970, None),
    # Workshops (not individuals)
    "atelier thanh le":        ("Xưởng Sơn Mài Thành Lễ",    None, None),
    "thanh le":                ("Xưởng Sơn Mài Thành Lễ",    None, None),
    "duc loi":                 ("Xưởng Sơn Mài Thành Lễ",    None, None),
    "atelier duc loi":         ("Xưởng Sơn Mài Thành Lễ",    None, None),
    "tran van ha":             ("Trần Văn Hà",               1911, 1974),
    "nguyen sien":             ("Nguyễn Siên",               1916, 2014),
    "le thanh":                ("Lê Thanh",                  1942, None),
    "le minh":                 ("Lê Minh",                   1937, None),
    "mai long":                ("Mai Long",                  1930, 2024),
    "nguyen khac chinh":       ("Nguyễn Khắc Chinh",         1984, None),
    "dinh trong khang":        ("Đinh Trọng Khang",          1931, None),  # father of Đinh Ý Nhi, Hanoi Fine Arts teacher
    "trinh cong son":          ("Trịnh Công Sơn",            1939, 2001),
    "le van mien":             ("Lê Văn Miến",               1873, 1943),
    "hoang hong cam":          ("Hoàng Hồng Cẩm",            1959, None),
    "le kim my":               ("Lê Kim Mỹ",                 None, None),
    "ta ty":                   ("Tạ Tỵ",                     1921, 2004),
    "hoang lap ngon":          ("Hoàng Lập Ngôn",            1910, 2006),
    "pham van chat":           ("Phạm Văn Chắt",             1916, None),
    "nguyen van anh":          ("Nguyễn Văn Anh",            1916, None),
    "luong xuan tam":          ("Lương Xuân Tâm",            None, None),
    "pham ngoc doanh":         ("Phạm Ngọc Doanh",           None, None),
    "tran hoang son":          ("Trần Hoàng Sơn",            1957, None),
    "mai van nam":             ("Mai Văn Nam",               None, None),
    "nguyen trung kien":       ("Nguyễn Trung Kiên",         None, None),
    "dam nhu khang":           ("Đàm Như Khang",             None, None),
    "phan ke an":              ("Phan Kế An",                1923, 2018),
    "trung nam":               ("Trung Nam",                 None, None),
    "nguyen van minh":         ("Nguyễn Văn Minh",           1925, 2004),
    "trinh ngoc":              ("Trịnh Ngọc",                None, None),
    "nguyen van ngoc":         ("Nguyễn Văn Ngọc",           None, None),
    "nguyen van tu":           ("Nguyễn Văn Tự",             None, None),
    "luong lan huong":         ("Lương Lan Hương",           None, None),
    "truong be":               ("Trương Bé",                 1942, None),
    "do hong quan":            ("Đỗ Hồng Quân",              None, None),
    "nguyen sang":             ("Nguyễn Sáng",               1923, 1988),
    "nguyen thi hien":         ("Nguyễn Thị Hiền",           1946, None),
    "ho thi xuan thu":         ("Hồ Thị Xuân Thu",           1960, None),
    "le quang dinh":           ("Lê Quang Định",             None, None),
    "vo xuong":                ("Võ Xương",                  None, None),
    "nguyen quoc huong":       ("Nguyễn Quốc Hương",         None, None),
    "luc ky":                  ("Lực Ký",                    None, None),
    "tran quoc giang":         ("Trần Quốc Giang",           1984, None),
    "do trung quan":           ("Đỗ Trung Quân",             1955, None),
}


# Known non-Vietnamese artists from SEA / regional that get picked up in dept crawls.
# Used to explicitly EXCLUDE.
NON_VN_EXCLUSIONS = {
    # Singapore
    "cheong soo pieng", "chua ek kay", "chen wen hsi", "siew hock meng",
    "liu kang", "georgette chen", "lim tze peng", "lim hak tai", "anthony poon",
    "tan choh tee", "ong kim seng", "tan oe pang",
    # Indonesia
    "mangku mura", "mangku muriati", "hendra gunawan", "gerard pieter adolfs",
    "antonio blanco", "but muchtar", "arie smit", "lee man fong",
    "affandi", "sudjojono", "fadjar sidik", "srihadi soedarsono",
    "sadali", "mochtar apin", "nashar", "basoeki abdullah", "trubus soedarsono",
    "agus suwage", "made wianta", "nyoman gunarsa", "i nyoman masriadi",
    "rudi mantofani", "handiwirman saputra", "entang wiharso",
    # Philippines
    "fernando cueto amorsolo", "juan luna", "fernando zobel", "hernando ocampo",
    "anita magsaysay-ho", "vicente manansala", "jose joya", "ang kiukok",
    "allan balisi", "kiko escora", "benedicto cabrera", "bencab",
    # Burma/Myanmar
    "u lun gywe", "paw oo thet", "u ba nyan", "u ngwe gaing",
    # Thailand
    "yuree kensaku", "misiem yipintsoi", "chakrabhand posayakrit",
    "pratuang emjaroen", "thawan duchanee",
    # Cambodia/Laos
    "sopheap pich", "svay ken", "marine ky",
    # China
    "xue song", "chen yifei", "zhang xiaogang", "fang lijun",
    "wang guangyi", "yue minjun", "zeng fanzhi", "liu xiaodong",
    # Europe/US (non-VN French dept.) — careful as some worked in Indochina
    "adrien-jean le mayeur de merprès", "adrien-jean le mayeur",
    "adrien jean le mayeur de merpres",
    "louis rollet", "charles jules duvent", "aladar farkas",
    # Japan
    "yayoi kusama",
    # Chinese-French / Chinese
    "t'ang haywen", "t ang haywen",
    # Burma
    "paw thame", "ngwe gaing", "saya saung",
    # Thailand
    "kid kosolawat",
    # Philippines
    "federico aguilar alcuaz",
    # Bali/Indonesia
    "auke sonnega",
    # Singapore Chinese
    "tay bak koi",
    # Swiss / not-Vietnamese (confusingly named "Mai-Thu")
    "mai thu perret", "mai-thu perret",
    # Non-artist catalog entries (objects)
    "plaque en jade a decor ajoure de dragons dynastie ming",
    "anonymous", "anonyme",
}


def is_vietnamese(normalized_key):
    """Check if a normalized artist name is in VN catalog (not in exclusions)."""
    if normalized_key in NON_VN_EXCLUSIONS:
        return False
    if normalized_key in VN_ARTIST_CATALOG:
        return True
    # Partial: maybe the name has extra suffix like "(1920-2014)" stripped
    # Check if any VN key is a prefix
    for k in VN_ARTIST_CATALOG:
        if normalized_key.startswith(k + " ") or k.startswith(normalized_key + " "):
            return True
    return False
