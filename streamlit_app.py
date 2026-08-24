import streamlit as st
import asyncio
import io
import openpyxl
import pandas as pd
from playwright_setup import ensure_playwright_installed
from directory_scanner import (
    DirectoryBlockedError,
    DirectoryThrottle,
    scan_directory_cities,
)
from lead_scanner import scan_leads

st.set_page_config(page_title="Airbnb Account & Listing Scanner", layout="wide")
st.title("🎯 Airbnb Account & Listing Scanner")
st.write(
    "Paste Airbnb co-host profiles or individual listing URLs to find publicly "
    "shared listings and open their locations in Google Maps."
)

st.divider()

if "playwright_ready" not in st.session_state:
    with st.spinner("🧰 Preparing browser runtime..."):
        st.session_state.playwright_ready = ensure_playwright_installed()

if not st.session_state.playwright_ready:
    st.error(
        "❌ Playwright browser setup failed in this runtime. "
        "If this is Streamlit Cloud, check app logs and redeploy."
    )
    st.stop()

# Input section
st.subheader("📝 Input Airbnb URLs")
col1, col2 = st.columns([3, 1])
with col1:
    urls_input = st.text_area(
        "Enter Airbnb co-host profile or listing URLs (one per line):",
        height=150,
        placeholder=(
            "https://www.airbnb.com/co-hosts/profile/123456789\n"
            "https://www.airbnb.com/rooms/987654321"
        )
    )
with col2:
    st.write("")
    st.write("")
    scan_button = st.button("🚀 Scan Accounts & Listings", use_container_width=True)

st.divider()


def _normalize_url(value: str) -> str:
    """Normalize user input so scanner always receives valid URLs."""
    clean = value.strip()
    if not clean:
        return ""
    if not clean.startswith(("http://", "https://")):
        clean = f"https://{clean}"
    return clean


def _safe_http_url(value) -> str:
    """Return a clickable URL only when the value is a valid absolute http(s) URL."""
    text = "" if value is None else str(value).strip()
    return text if text.startswith(("http://", "https://")) else ""


def _is_auth_block_error(error_text: str) -> bool:
    """Detect auth-block errors so the UI can avoid noisy raw exception output."""
    text = (error_text or "").lower()
    return any(token in text for token in ["401", "403", "unauthorized", "forbidden", "http_401", "http_403"])


def _run_scan(urls):
    """Run async scanner safely across Streamlit reruns/event-loop states."""
    try:
        return asyncio.run(scan_leads(urls))
    except RuntimeError as e:
        if "asyncio.run() cannot be called from a running event loop" not in str(e):
            raise
        loop = asyncio.get_event_loop()
        return loop.run_until_complete(scan_leads(urls))

# Process URLs
if scan_button:
    if not urls_input.strip():
        st.error("Please enter at least one URL to scan.")
    else:
        urls = [_normalize_url(url) for url in urls_input.split('\n') if url.strip()]
        st.info(
            f"Scanning {len(urls)} Airbnb account/listing URL(s)... "
            "This may take a few minutes."
        )
        
        try:
            with st.spinner("🔍 Finding account listings and public map locations..."):
                results_df = _run_scan(urls)

            scan_stats = results_df.attrs.get("scan_stats", {})
            profile_summaries = results_df.attrs.get("profile_summaries", [])
            blocked_by_airbnb = bool(results_df.attrs.get("blocked_by_airbnb", False))
            fatal_error = str(results_df.attrs.get("fatal_error", "") or "").strip()

            if fatal_error:
                st.warning(
                    "Scanner recovered from a runtime failure and kept the server running. "
                    f"Details: {fatal_error}"
                )

            if blocked_by_airbnb:
                st.warning(
                    "Airbnb blocked at least one profile or listing lookup. "
                    "Any public results that were available are shown below."
                )

            if scan_stats:
                st.caption(
                    "Scanner heartbeat: "
                    f"accounts {scan_stats.get('profiles_total', 0)}, "
                    f"account-reported listings {scan_stats.get('reported_listings_total', 0)}, "
                    f"public listing links found {scan_stats.get('public_listing_links_found', 0)}, "
                    f"listings scanned {scan_stats.get('listings_total', 0)}, "
                    f"mapped {scan_stats.get('listings_mapped', 0)}, "
                    f"without a public location {scan_stats.get('listings_without_location', 0)}, "
                    f"invalid {scan_stats.get('listings_invalid', 0)}, "
                    f"Airbnb blocked {scan_stats.get('listings_auth_blocked', 0)}, "
                    f"errors {scan_stats.get('listing_errors', 0)}"
                )
            
            if profile_summaries:
                profile_df = pd.DataFrame(profile_summaries)
                if "Account Profile" in profile_df.columns:
                    profile_df["Account Profile"] = profile_df["Account Profile"].apply(
                        _safe_http_url
                    )
                st.subheader("👤 Account Summary")
                st.caption(
                    "Reported Listings comes from Airbnb's account page. Public Listing "
                    "Links Found shows how many listing pages Airbnb exposed to the scanner."
                )
                st.dataframe(
                    profile_df,
                    use_container_width=True,
                    column_config={
                        "Account Profile": st.column_config.LinkColumn(
                            "Airbnb Account", display_text="Open account"
                        )
                    },
                )

            if len(results_df) > 0:
                st.success(
                    f"✅ Mapped {scan_stats.get('listings_mapped', 0)} of "
                    f"{scan_stats.get('listings_total', 0)} public listings!"
                )

                # Prevent invalid values from being rendered as broken links.
                if "Account Profile" in results_df.columns:
                    results_df["Account Profile"] = results_df["Account Profile"].apply(
                        _safe_http_url
                    )
                if "Maps" in results_df.columns:
                    results_df["Maps"] = results_df["Maps"].apply(_safe_http_url)
                if "Link" in results_df.columns:
                    results_df["Link"] = results_df["Link"].apply(_safe_http_url)
                
                # Display results
                st.subheader("📊 Results")
                st.caption(
                    "Airbnb may publish an approximate map pin rather than an exact address. "
                    "Use the Location Precision column and manually verify every result."
                )
                st.dataframe(
                    results_df,
                    use_container_width=True,
                    column_config={
                        "Account Profile": st.column_config.LinkColumn(
                            "Airbnb Account", display_text="Open account"
                        ),
                        "Maps": st.column_config.LinkColumn("Google Maps", display_text="Open map"),
                        "Link": st.column_config.LinkColumn("Airbnb Listing", display_text="Open listing"),
                    },
                )
                
                # Export options
                col1, col2 = st.columns(2)
                with col1:
                    csv = results_df.to_csv(index=False)
                    st.download_button(
                        label="📥 Download CSV",
                        data=csv,
                        file_name="airbnb_listing_locations.csv",
                        mime="text/csv"
                    )
                with col2:
                    try: 
                        excel_buffer = io.BytesIO()
                        results_df.to_excel(excel_buffer, index=False, engine='openpyxl')
                        excel_buffer.seek(0)
                        st.download_button(
                            label="📥 Download Excel",
                            data=excel_buffer.getvalue(),
                            file_name="airbnb_listing_locations.xlsx",
                            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                        )
                    except Exception as e:
                        st.error(f"Excel export failed: {e}")
            else:
                if not blocked_by_airbnb:
                    st.warning("⚠️ No public Airbnb listing links were found.")
                
        except Exception as e:
            err_text = str(e)
            if _is_auth_block_error(err_text):
                st.warning(
                    "Airbnb is currently blocking this runtime (401/403). "
                    "The app is still running. Try again later or use a different network/IP."
                )
            else:
                st.error(f"❌ Error during scanning: {err_text}")
                st.write("Please check your URLs and try again.")


st.divider()
st.subheader("🏙️ Airbnb Directory City Counter")
st.write(
    "Paste city/state pairs to count the profiles Airbnb currently reports in "
    "its public co-host directory. Run large lists in small batches."
)

directory_input = st.text_area(
    "Enter cities as City, State (one per line):",
    height=150,
    placeholder="Lihue, HI\nOrlando, FL\nAlexandria, VA",
    key="directory_city_input",
)

throttle_col, batch_col, pause_col = st.columns(3)
with throttle_col:
    directory_delay = st.number_input(
        "Minimum delay between cities (seconds)",
        min_value=5,
        max_value=30,
        value=8,
        step=1,
        help="The scanner adds up to 6 additional random seconds.",
    )
with batch_col:
    directory_batch_size = st.number_input(
        "Cities before a cooldown",
        min_value=5,
        max_value=25,
        value=20,
        step=1,
    )
with pause_col:
    directory_cooldown = st.number_input(
        "Cooldown between batches (seconds)",
        min_value=30,
        max_value=180,
        value=60,
        step=5,
    )

st.caption(
    "Safety controls: sequential requests only, randomized delays, exponential "
    "backoff, Retry-After support, periodic cooldowns, and an automatic stop "
    "after repeated 403/429 responses. These reduce risk but cannot guarantee "
    "Airbnb will never limit a scan."
)


def _parse_city_state_lines(value: str):
    cities = []
    seen = set()
    errors = []
    for line_number, raw_line in enumerate((value or "").splitlines(), start=1):
        clean = raw_line.strip()
        if not clean:
            continue
        if "\t" in clean:
            parts = [part.strip() for part in clean.split("\t") if part.strip()]
        else:
            parts = [part.strip() for part in clean.rsplit(",", 1)]
        if len(parts) != 2 or not all(parts):
            errors.append(line_number)
            continue
        city, state = parts
        key = (city.casefold(), state.casefold())
        if key not in seen:
            seen.add(key)
            cities.append((city, state.upper()))
    return cities, errors


if st.button("🔎 Count Directory Profiles", use_container_width=True):
    directory_cities, directory_errors = _parse_city_state_lines(directory_input)
    if directory_errors:
        st.error(
            "Use one City, State pair per line. Check line(s): "
            + ", ".join(str(line) for line in directory_errors)
        )
    elif not directory_cities:
        st.error("Enter at least one city and state.")
    elif len(directory_cities) > 50:
        st.error(
            f"This run contains {len(directory_cities)} cities. "
            "For safer pacing, use batches of 50 or fewer."
        )
    else:
        throttle = DirectoryThrottle(
            min_delay_seconds=float(directory_delay),
            max_delay_seconds=float(directory_delay) + 6.0,
            batch_size=int(directory_batch_size),
            batch_pause_min_seconds=float(directory_cooldown),
            batch_pause_max_seconds=float(directory_cooldown) + 30.0,
            max_retries=3,
            retry_base_seconds=30.0,
        )
        directory_progress = st.progress(0.0)
        directory_status = st.empty()

        def _directory_progress_update(index, total, result):
            directory_progress.progress(index / total)
            directory_status.caption(
                f"Checked {index} of {total}: "
                f"{result.get('City')}, {result.get('State')} — "
                f"{result.get('Status')}"
            )

        try:
            directory_results = scan_directory_cities(
                directory_cities,
                throttle=throttle,
                progress_callback=_directory_progress_update,
            )
            directory_df = pd.DataFrame(directory_results)
            st.success(
                f"Completed {len(directory_df)} city lookup(s). "
                "Counts reflect the directory at scan time."
            )
            st.dataframe(
                directory_df,
                use_container_width=True,
                column_config={
                    "Directory URL": st.column_config.LinkColumn(
                        "Airbnb Directory", display_text="Open directory"
                    )
                },
            )
            st.download_button(
                "📥 Download Directory Counts",
                data=directory_df.to_csv(index=False),
                file_name="airbnb_directory_city_counts.csv",
                mime="text/csv",
            )
        except DirectoryBlockedError:
            st.warning(
                "Airbnb returned repeated block or rate-limit responses, so the "
                "scan stopped automatically. Wait before starting another batch."
            )
