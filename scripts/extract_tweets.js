(function(){
  const arts = document.querySelectorAll("article[data-testid='tweet']");
  const out = [];
  arts.forEach(function(a) {
    const links = a.querySelectorAll("a[href*='/status/']");
    let id = "";
    for (let i = 0; i < links.length; i++) {
      const h = links[i].href || "";
      if (h.includes("/status/")) {
        const p = h.split("/status/");
        if (p.length > 1) {
          id = p[1].split("?")[0].split("/")[0];
          if (/^\d+$/.test(id)) break;
        }
      }
    }
    if (!id) return;
    const texts = a.querySelectorAll("div[data-testid='tweetText']");
    let txt = "";
    for (let i = 0; i < texts.length; i++) txt += texts[i].innerText + " ";
    txt = txt.trim();
    const t = a.querySelector("time");
    const dt = t ? t.getAttribute("datetime") : "";
    const n = a.querySelector("div[data-testid='User-Name']");
    const dn = n ? n.innerText.split("\n")[0] : "";
    out.push({tweet_id: id, text: txt, created_at: dt, display_name: dn, is_reply: !!a.querySelector("div[data-testid='tweetReplyContext']"), is_retweet: !!a.querySelector("span[data-testid='socialContext']")});
  });
  localStorage.setItem("__scraped_tweets", JSON.stringify(out));
})();
