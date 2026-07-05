# Speed Camera System

AI-powered license plate detection, speed tracking, and traffic fine generator.

## Features

- **License Plate Detection** — YOLOv9-t ONNX model via `fast-alpr`, with OpenCV fallback
- **OCR** — Built-in ViT OCR from `fast-alpr` + EasyOCR backup
- **Speed Detection** — Track vehicle speed across video frames
- **Fine Generation** — Automatic fine issuance with configurable speed limits
- **Vehicle Lookup** — Real data from Sindh excise portal via 2captcha CAPTCHA solving; region-aware mock data fallback
- **Manual Correction** — Correct vehicle details on any fine (SMS your plate to 8785 for real data)

## Quick Start

```bash
pip install -r requirements.txt
python app.py
```

Open http://127.0.0.1:5000

## Usage

| Route | Description |
|-------|-------------|
| `/live` | Real-time webcam detection |
| `/upload` | Detect plate from photo |
| `/manual` | Enter plate + speed manually |
| `/fines` | View all issued fines |
| `/settings` | Configure 2captcha API key |

## Configuration

Set a **2captcha API key** in Settings (`/settings`) to enable automatic real vehicle data lookup from the Sindh excise portal. Without it, region-aware mock data is used.

## API Key

- `Sindh / Karachi K-series` → excise.gos.pk
- `Punjab L-series` → mtmis.excise.punjab.gov.pk
