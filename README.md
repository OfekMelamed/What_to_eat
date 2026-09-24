# ביס 🍽️

אפליקציית החלקות לבחירת מקום לאכול. דף אחד (`index.html`), רץ על GitHub Pages, בחינם.

## מאיפה מגיעים המקומות
1. **מאגר ערים משלך** (`data/`). נבנה עם `scripts/build_city.py` מ-Overture Maps
   (כ-81 מיליון מקומות בעולם), בתוספת שעות וכשרות מ-OpenStreetMap ותמונות מהאתרים של המקומות.
2. **אם את/ה בעיר שאין לה מאגר:** משיכה חיה מ-OpenStreetMap (פחות מקומות).

## הקמה (פעם אחת)
1. מעלים את כל התיקייה ל-repo חדש ב-GitHub.
2. Settings → Pages → Branch: `main`, תיקייה `/ (root)` → Save.
3. Settings → Actions → General → Workflow permissions → **Read and write** → Save.

## הוספת עיר
Actions → **בניית מאגר מסעדות** → Run workflow → כותבים שם עיר (למשל `תל אביב` או `Rome, Italy`) → Run.
אחרי כמה דקות נוצר `data/<עיר>.json`, והאפליקציה משתמשת בו אוטומטית כשהטלפון נמצא בעיר הזו.
כל הערים ב-`cities.txt` מתרעננות לבד ב-1 לכל חודש.

רוצים רק שכונה ולא את כל העיר? ממלאים גם "רדיוס" (למשל `3`).

## הרצה מקומית (WSL)
```bash
pip install -r scripts/requirements.txt
python scripts/build_city.py "תל אביב"            # כל העיר
python scripts/build_city.py "חיפה" --radius-km 4  # 4 ק״מ סביב המרכז
python -m http.server 8000                         # ואז http://localhost:8000
```

## התאמה אישית
בראש הסקריפט ב-`index.html` יש בלוק `CONFIG`: השם שלה, ההקדשה, החתימה ומקומות מיוחדים.

## קרדיטים
מידע: Overture Maps Foundation (CDLA-Permissive-2.0), © OpenStreetMap contributors (ODbL).
תמונות להמחשה: Unsplash. תמונות מאתרי המקומות שייכות לבעליהן, והשימוש כאן אישי ולא מסחרי.
