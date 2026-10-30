const refresh = async () => {
	try {
		const response = await fetch("/api/now-playing", { cache: "no-store", signal: AbortSignal.timeout(5000) });
		if (!response.ok) {
			throw new Error("Unavailable");
		}
		const song = await response.json();
		const cover = document.getElementById("cover");
		const artwork = song.state !== "stopped" && /^\/api\/artwork\/[a-f0-9]{64}$/u.test(song.artwork || "") ? song.artwork : null;
		cover.hidden = !artwork;
		if (artwork) {
			if (cover.getAttribute("src") !== artwork) {
				cover.src = artwork;
			}
			cover.alt = song.album ? `${song.album} album cover` : "Album cover";
			cover.onerror = () => { cover.hidden = true; };
		} else {
			cover.removeAttribute("src");
		}
		document.getElementById("title").textContent = song.title || (song.state === "stopped" ? "Nothing playing" : song.source_label);
		document.getElementById("artist").textContent = song.artist;
		document.getElementById("album").textContent = song.album;
		document.getElementById("state").textContent = `${song.source_label} · ${song.state}${song.state === "playing" && !song.title ? " · Song details unavailable" : ""}`;
	} catch {
		document.getElementById("cover").hidden = true;
		document.getElementById("cover").removeAttribute("src");
		document.getElementById("title").textContent = "Octet unavailable";
		for (const id of ["artist", "album", "state"]) {
			document.getElementById(id).textContent = "";
		}
	}
	setTimeout(refresh, 3000);
};
refresh();
