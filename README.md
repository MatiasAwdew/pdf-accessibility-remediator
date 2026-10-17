# Document Accessibility Remediator

A tool I built that makes PDFs and Word documents accessible (Section 508, WCAG 2.1 AA, PDF/UA-1).
It fixes the repetitive problems automatically, writes alt text and table descriptions with AI, and
leaves only the judgment calls for a person to review.

*The source code is private. This page shows what the tool does and the results. I'm happy to walk
through the code or give a live demo; message me here on GitHub or on LinkedIn.*

![Before and after: a sample report fails 9 Word checks and 11 PDF/UA checks before, and passes all of them after](images/before-after.png)

## What it does

**PDFs**
- Tags untagged PDFs and repairs existing tag trees: headings, lists, tables (header cells, scope,
  summaries), links, reading order, artifacts
- Writes figure alt text, document titles and table summaries with the Claude API, and learns from
  the reviewer's past corrections
- Fixes fonts, metadata, language, contrast, bookmarks and tab order
- Validates every file with veraPDF (PDF/UA-1) and keeps fixing until it passes, or reports exactly
  what is left

**Word documents (.docx)**
- Turns bold "fake" headings into real heading styles, typed bullets into real lists, and
  tab-aligned text into tables
- Sets table header rows (repeating on each page), removes blank rows, splits tables that use
  merged rows as section titles
- Writes alt text for pictures and charts, fixes vague link text, low contrast and empty spacing lines,
  moves text boxes into reading order, and sets the title and language
- Checks the result against both **Word's Accessibility Checker** rules and the **HHS Section 508
  Word checklist**, and can run Word's own checker to confirm

**Built for review, not blind automation.** Every judgment call (alt text, heading levels, link
wording, table descriptions) appears in a review screen where a person can accept, edit or reject it
before downloading the final file.

## Results

| Test | Result |
|---|---|
| Sample report in this repo, Word version | Fails 9 checks before; passes Word's checker rules + HHS 508 after |
| Sample report in this repo, untagged PDF | Fails 11 PDF/UA checks before; passes veraPDF PDF/UA-1 after |
| 25 unusual Word test files (Apache POI test suite: forms, charts, nested tables, old Word formats) | 21 of 25 pass both standards, 0 crashes; the other 4 need a person to rebuild their layout, and the tool says why |
| Real documents I remediated with it | Pass the Acrobat Accessibility Checker and PAC 2026 |
| Automated tests | 89 |

Try it yourself: [`samples/`](samples) has the sample report before and after, as a Word file and as a PDF.
Open them in Word (Review > Check Accessibility), Acrobat, or PAC.

![Page 1 of the sample report](images/sample-page.png)

## How it works

1. Upload a PDF or Word file in the web app (or run it from the command line on a whole folder).
2. The tool fixes what it can, asks Claude for alt text and descriptions, and validates the result.
3. A person reviews the judgment calls, clicks **Apply my decisions**, and downloads the file.
4. A report lists what was fixed, what was reviewed, and anything that still needs manual work.

## Built with

Python, pikepdf, pdfplumber, PyMuPDF, fontTools, python-docx / lxml, Flask, the Claude API,
the Adobe PDF Services API, veraPDF, Docker, and Windows UI Automation (to run Word's own
Accessibility Checker).

---

Built by Matias Awdew. The sample report is fictional.
