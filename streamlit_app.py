import streamlit as st
import asyncio
import io
import openpyxl
from playwright_setup import ensure_playwright_installed
from lead_scanner import scan_leads

st.set_page_config(page_title="Lead Scanner", layout="wide")
st.title("🎯 Lead Generation Scanner")
st.write("Scan Airbnb host profiles to identify potential leads with property information.")

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
st.subheader("📝 Input Host URLs")
col1, col2 = st.columns([3, 1])
with col1:
    urls_input = st.text_area(
        "Enter Airbnb host profile URLs (one per line):",
        height=150,
        placeholder="https://www.airbnb.com/users/show/123456789\nhttps://www.airbnb.com/users/show/987654321"
    )
with col2:
    st.write("")
    st.write("")
    scan_button = st.button("🚀 Scan Leads", use_container_width=True)

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
        st.info(f"Scanning {len(urls)} host profile(s)... This may take a few minutes.")
        
        try:
            with st.spinner("🔍 Scanning profiles..."):
                results_df = _run_scan(urls)

            scan_stats = results_df.attrs.get("scan_stats", {})
            blocked_by_airbnb = bool(results_df.attrs.get("blocked_by_airbnb", False))
            fatal_error = str(results_df.attrs.get("fatal_error", "") or "").strip()
            has_public_fallback_links = (
                "Address" in results_df.columns
                and results_df["Address"].astype(str).str.contains("Public .*link only", case=False, regex=True).any()
            )

            if fatal_error:
                st.warning(
                    "Scanner recovered from a runtime failure and kept the server running. "
                    f"Details: {fatal_error}"
                )

            if blocked_by_airbnb:
                if has_public_fallback_links:
                    st.warning(
                        "Airbnb is returning 401/403 for this runtime. "
                        "Public Airbnb links are shown below, but full lead enrichment is temporarily blocked."
                    )
                else:
                    st.warning(
                        "Airbnb is currently returning 401/403 for this runtime, so no leads could be fetched right now. "
                        "Try again later or use a different network/IP."
                    )

            if scan_stats:
                st.caption(
                    "Scanner heartbeat: "
                    f"hosts total {scan_stats.get('hosts_total', 0)}, "
                    f"hosts scanned {scan_stats.get('hosts_scanned', 0)}, "
                    f"hosts skipped {scan_stats.get('hosts_skipped', 0)}, "
                    f"hosts auth blocked {scan_stats.get('hosts_auth_blocked', 0)}, "
                    f"rooms scanned {scan_stats.get('rooms_scanned', 0)}, "
                    f"rooms skipped {scan_stats.get('rooms_skipped', 0)}, "
                    f"rooms auth blocked {scan_stats.get('rooms_auth_blocked', 0)}"
                )
            
            if len(results_df) > 0:
                st.success(f"✅ Found {len(results_df)} leads!")

                # Prevent invalid values (e.g., "N/A") from being rendered as broken links.
                if "Maps" in results_df.columns:
                    results_df["Maps"] = results_df["Maps"].apply(_safe_http_url)
                if "Link" in results_df.columns:
                    results_df["Link"] = results_df["Link"].apply(_safe_http_url)
                
                # Display results
                st.subheader("📊 Results")
                st.dataframe(
                    results_df,
                    use_container_width=True,
                    column_config={
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
                        file_name="leads.csv",
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
                            file_name="leads.xlsx",
                            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                        )
                    except Exception as e:
                        st.error(f"Excel export failed: {e}")
            else:
                if not blocked_by_airbnb:
                    st.warning("⚠️ No leads found. The profiles may not match the target criteria.")
                
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
