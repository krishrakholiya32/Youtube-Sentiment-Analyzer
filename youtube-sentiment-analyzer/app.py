import streamlit as st
import pandas as pd
import plotly.express as px
from youtube_comment_downloader import YoutubeCommentDownloader, SORT_BY_POPULAR
from groq import Groq
import os
import json
import time
import re
from dotenv import load_dotenv

load_dotenv()

st.set_page_config(page_title="YouTube Sentiment Analyzer", page_icon="📊", layout="wide")

st.title("📊 YouTube Comment Sentiment Analyzer")
st.markdown("Enter a YouTube video URL to analyze the sentiment of its comments using Groq AI.")

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
if not GROQ_API_KEY:
    st.error("Groq API key not found. Please set GROQ_API_KEY in your .env file.")
    st.stop()


def extract_video_id(url_or_id):
    if "youtube.com" in url_or_id or "youtu.be" in url_or_id:
        if "v=" in url_or_id:
            return url_or_id.split("v=")[1].split("&")[0]
        elif "youtu.be/" in url_or_id:
            return url_or_id.split("youtu.be/")[1].split("?")[0]
    return url_or_id


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_comments(video_url, limit):
    downloader = YoutubeCommentDownloader()
    comments = []
    try:
        for comment in downloader.get_comments_from_url(video_url, sort_by=SORT_BY_POPULAR):
            text = comment.get("text", "").strip()
            if text:
                comments.append(text[:512])
            if len(comments) >= limit:
                break
    except Exception as e:
        st.error(f"Failed to fetch comments: {e}")
        return []
    return comments


@st.cache_resource
def get_groq_client():
    return Groq(api_key=GROQ_API_KEY)


def analyze_batch(client, comments_batch):
    """
    Send up to 20 comments to Groq LLaMA and get back JSON sentiment results.
    Retries up to 3 times on parse failure.
    """
    numbered = "\n".join([f"{i+1}. {c}" for i, c in enumerate(comments_batch)])

    prompt = f"""Analyze the sentiment of each YouTube comment below.
For EVERY comment return a JSON array with exactly {len(comments_batch)} objects in the same order.
Each object must have:
  - "sentiment": one of "POSITIVE", "NEGATIVE", or "NEUTRAL"
  - "confidence": a float between 0.0 and 1.0
  - "reason": a short 1-sentence explanation (no special characters, no quotes, no newlines inside it)

IMPORTANT:
- Handle sarcasm, slang, emojis, and mixed sentiment carefully.
- Keep the "reason" field simple — no commas, no inner quotes, no newlines.
- Return ONLY the raw JSON array. No markdown, no backticks, no explanation outside the array.

Comments:
{numbered}"""

    for attempt in range(3):
        try:
            response = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
            )
            raw = response.choices[0].message.content.strip()

            # Strip markdown code fences
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)

            # Extract just the JSON array in case model adds extra text
            match = re.search(r"\[.*\]", raw, re.DOTALL)
            if match:
                raw = match.group(0)

            results = json.loads(raw)

            if not isinstance(results, list):
                raise ValueError("Response is not a JSON array")

            # Trim if too many, pad if too few
            if len(results) > len(comments_batch):
                results = results[:len(comments_batch)]
            while len(results) < len(comments_batch):
                results.append({"sentiment": "NEUTRAL", "confidence": 0.5, "reason": "Analysis incomplete"})

            for r in results:
                r["sentiment"] = r.get("sentiment", "NEUTRAL").upper()
                if r["sentiment"] not in ("POSITIVE", "NEGATIVE", "NEUTRAL"):
                    r["sentiment"] = "NEUTRAL"
                r["confidence"] = max(0.0, min(1.0, float(r.get("confidence", 0.5))))
                r["reason"] = r.get("reason", "")

            return results

        except Exception as e:
            if attempt < 2:
                time.sleep(2)
                continue
            st.warning(f"Batch failed after 3 attempts ({e}), marking {len(comments_batch)} comments as NEUTRAL.")
            return [{"sentiment": "NEUTRAL", "confidence": 0.5, "reason": "Analysis failed"} for _ in comments_batch]


# ── UI ───────────────────────────────────────────────────────────────────────

video_input = st.text_input(
    "YouTube Video URL",
    placeholder="https://www.youtube.com/watch?v=dQw4w9WgXcQ"
)
limit = st.slider("Number of comments to analyze", min_value=20, max_value=500, value=100, step=20)

if limit > 300:
    st.info("⏱️ Analyzing many comments takes a bit longer. Please be patient.")

if st.button("Analyze Sentiment", type="primary") and video_input:
    video_id = extract_video_id(video_input)
    start_time = time.time()

    # Step 1: Fetch comments
    fetch_progress = st.progress(0, text="Fetching comments...")
    comments = fetch_comments(video_input, limit)
    fetch_progress.progress(1.0, text=f"Fetched {len(comments)} comments!")
    fetch_progress.empty()

    if not comments:
        st.warning("No comments found. Make sure the video is public and comments are enabled.")
        st.stop()

    # Step 2: Analyze in batches of 20
    client = get_groq_client()
    batch_size = 20
    data = []
    sentiment_progress = st.progress(0, text="Analyzing sentiment with Groq AI...")

    for i in range(0, len(comments), batch_size):
        batch = comments[i:i + batch_size]
        results = analyze_batch(client, batch)

        for comment, result in zip(batch, results):
            data.append({
                "comment": comment,
                "sentiment": result["sentiment"],
                "confidence": result["confidence"],
                "reason": result["reason"],
            })

        analyzed = min(i + batch_size, len(comments))
        sentiment_progress.progress(analyzed / len(comments), f"Analyzed {analyzed}/{len(comments)}")

        # Groq free tier: 30 req/min → 2s gap is safe
        if i + batch_size < len(comments):
            time.sleep(2)

    sentiment_progress.empty()
    df = pd.DataFrame(data)

    elapsed = time.time() - start_time
    st.success(f"✅ Analysis completed in {elapsed:.1f} seconds!")

    # ── Metrics ──────────────────────────────────────────────────────────────
    st.subheader(f"Video ID: `{video_id}`")
    total = len(df)
    pos   = len(df[df.sentiment == "POSITIVE"])
    neg   = len(df[df.sentiment == "NEGATIVE"])
    neu   = len(df[df.sentiment == "NEUTRAL"])

    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("Total Comments", total)
    with col2:
        st.metric("😊 Positive", f"{pos} ({pos/total:.1%})")
    with col3:
        st.metric("😞 Negative", f"{neg} ({neg/total:.1%})")
    with col4:
        st.metric("😐 Neutral", f"{neu} ({neu/total:.1%})")

    # ── Charts ───────────────────────────────────────────────────────────────
    counts = df.sentiment.value_counts().reset_index()
    counts.columns = ["Sentiment", "Count"]
    color_map = {"POSITIVE": "#22c55e", "NEGATIVE": "#ef4444", "NEUTRAL": "#94a3b8"}

    col1, col2 = st.columns(2)

    with col1:
        st.subheader("📈 Sentiment Distribution")
        fig_bar = px.bar(
            counts, x="Sentiment", y="Count", color="Sentiment",
            color_discrete_map=color_map
        )
        fig_bar.update_layout(showlegend=False)
        st.plotly_chart(fig_bar, use_container_width=True)

    with col2:
        st.subheader("🥧 Sentiment Share")
        fig_pie = px.pie(
            counts, values="Count", names="Sentiment",
            color="Sentiment", color_discrete_map=color_map
        )
        st.plotly_chart(fig_pie, use_container_width=True)

    # ── Confidence distribution ───────────────────────────────────────────────
    st.subheader("📊 Confidence Distribution")
    fig_hist = px.histogram(
        df, x="confidence", color="sentiment", nbins=20,
        color_discrete_map=color_map,
        barmode="overlay", opacity=0.7
    )
    st.plotly_chart(fig_hist, use_container_width=True)

    # ── Sample comments by sentiment ──────────────────────────────────────────
    st.subheader("💬 Sample Comments")
    tab1, tab2, tab3 = st.tabs(["😊 Positive", "😞 Negative", "😐 Neutral"])

    with tab1:
        st.dataframe(
            df[df.sentiment == "POSITIVE"][["comment", "confidence", "reason"]].head(10),
            use_container_width=True
        )
    with tab2:
        st.dataframe(
            df[df.sentiment == "NEGATIVE"][["comment", "confidence", "reason"]].head(10),
            use_container_width=True
        )
    with tab3:
        st.dataframe(
            df[df.sentiment == "NEUTRAL"][["comment", "confidence", "reason"]].head(10),
            use_container_width=True
        )

    # ── Full table ────────────────────────────────────────────────────────────
    with st.expander("View all comments"):
        st.dataframe(
            df[["comment", "sentiment", "confidence", "reason"]],
            use_container_width=True
        )

    # ── Download ──────────────────────────────────────────────────────────────
    csv = df.to_csv(index=False).encode("utf-8")
    st.download_button("📥 Download CSV", csv, "youtube_sentiment.csv", "text/csv")