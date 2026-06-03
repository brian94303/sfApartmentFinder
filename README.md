# SF Apartment Finder

SF Apartment Finder is a FastAPI web app for finding rental apartments near Mission Bay in San Francisco, centered around the OpenAI office area.

The app shows nearby rental listings on a map and in a sidebar, making it easy to compare apartments by location, rent, and bedroom count.

## Features

- Find rental apartments near Mission Bay
- View listings on an interactive Google Map
- Sort apartments by distance to the office
- Sort apartments by rent
- Filter listings by number of bedrooms
- See listing details such as price, bedrooms, bathrooms, square footage, and distance
- Open listing links and walking directions
- Includes a buy page for nearby for-sale listings

## Tech Stack

- FastAPI
- Uvicorn
- Vanilla HTML, CSS, and JavaScript
- Google Maps JavaScript API
- Redfin data through RapidAPI

## Setup

Create a virtual environment and install dependencies:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Create a `.env` file:

```bash
RAPIDAPI_KEY=your_rapidapi_key
MAPS_API_KEY=your_google_maps_api_key
```

Run the app:

```bash
uvicorn main:app --reload
```

Open:

```text
http://127.0.0.1:8000/
```

Buy page:

```text
http://127.0.0.1:8000/buy
```

## Notes

If `RAPIDAPI_KEY` is not set, the app falls back to sample data. If `MAPS_API_KEY` is not set, listings can still load, but the map will not render.
