# Evolve BPO Airbnb Listing Location Tool

A Streamlit tool that turns public Airbnb listing URLs into clickable Google Maps locations.

## Features

- Accepts one or more Airbnb `/rooms/<listing-id>` URLs.
- Extracts the listing's publicly shared latitude and longitude.
- Labels the public pin as exact or approximate when Airbnb supplies that metadata.
- Reverse-geocodes the public pin into a reviewable address.
- Provides direct Airbnb and Google Maps links.
- Exports the results to CSV or Excel.

Airbnb may show an approximate public map pin instead of an exact property location. The
tool reports the public pin's precision and does not present an approximate result as a
confirmed exact address.

## Installation

1. Clone the repository.
2. Install the dependencies:

   ```bash
   pip install -r requirements.txt
   ```

3. Install the Playwright browser:

   ```bash
   playwright install chromium
   ```

## Usage

```bash
streamlit run streamlit_app.py
```

Paste one Airbnb listing URL per line and select **Scan Leads**.

Example:

```text
https://www.airbnb.com/rooms/1699533136478015821
https://www.airbnb.com/rooms/602028875740552381
```

## Tests

```bash
python -m unittest discover -s tests -v
```

## License

See [LICENSE](LICENSE).
