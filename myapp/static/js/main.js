document.querySelectorAll("[data-dialog-open]").forEach((button) => {
	button.addEventListener("click", () => {
		document.getElementById(button.dataset.dialogOpen)?.showModal();
	});
});

document.querySelectorAll("[data-dialog-close]").forEach((button) => {
	button.addEventListener("click", () => button.closest("dialog")?.close());
});

document.querySelectorAll("dialog.checkout-dialog").forEach((dialog) => {
	dialog.addEventListener("click", (event) => {
		if (event.target === dialog) dialog.close();
	});
});

const cartDrawer = document.getElementById("cart-drawer");
if (cartDrawer) {
	const cartItems = cartDrawer.querySelector("[data-cart-items]");
	const cartEmpty = cartDrawer.querySelector("[data-cart-empty]");
	const cartStatus = cartDrawer.querySelector("[data-cart-status]");
	const csrfToken = cartDrawer.querySelector("[name='csrfmiddlewaretoken']")?.value;
	const coinToggle = cartDrawer.querySelector("[data-coin-toggle]");
	const redeemCoinsInput = cartDrawer.querySelector("[data-redeem-coins]");
	const money = (amount) => `KES ${Number(amount || 0).toLocaleString("en-KE", { minimumFractionDigits: 0, maximumFractionDigits: 2 })}`;
	let currentCart = { items: [], count: 0, total: "0", max_coin_discount: 0, coin_balance: 0 };

	const setDrawer = (open) => {
		cartDrawer.classList.toggle("is-open", open);
		cartDrawer.setAttribute("aria-hidden", String(!open));
		document.querySelector("[data-cart-open]")?.setAttribute("aria-expanded", String(open));
		document.body.classList.toggle("cart-open", open);
	};

	const renderCart = (cart) => {
		currentCart = cart;
		cartItems.replaceChildren();
		const items = cart.items || [];
		cartEmpty.hidden = items.length > 0;
		document.querySelectorAll("[data-cart-count]").forEach((node) => { node.textContent = cart.count || 0; });
		document.querySelectorAll("[data-cart-total]").forEach((node) => { node.textContent = money(cart.total); });
		document.querySelectorAll("[data-cart-subtotal]").forEach((node) => { node.textContent = money(cart.subtotal ?? cart.total); });
		cartDrawer.querySelectorAll("[data-coins-balance]").forEach((node) => { node.textContent = cart.coin_balance || 0; });
		updatePayableTotal();
		const checkoutButton = cartDrawer.querySelector("[data-cart-checkout]");
		if (checkoutButton) checkoutButton.disabled = !items.length;

		items.forEach((item) => {
			const row = document.createElement("article");
			row.className = "cart-item";
			if (item.image) {
				const image = document.createElement("img");
				image.src = item.image;
				image.alt = "";
				row.append(image);
			} else {
				const placeholder = document.createElement("div");
				placeholder.className = "cart-item-placeholder";
				placeholder.textContent = item.is_ticket ? "T" : "D";
				row.append(placeholder);
			}
			const details = document.createElement("div");
			details.className = "cart-item-details";
			const title = document.createElement("strong");
			title.textContent = item.name;
			const subtotal = document.createElement("span");
			subtotal.textContent = money(Number(item.price) * item.qty);
			const controls = document.createElement("div");
			controls.className = "cart-quantity";
			[["−", -1, "Decrease quantity"], ["+", 1, "Increase quantity"]].forEach(([label, change, ariaLabel]) => {
				const button = document.createElement("button");
				button.type = "button";
				button.textContent = label;
				button.setAttribute("aria-label", ariaLabel);
				button.dataset.cartChange = change;
				button.dataset.productId = item.product_id;
				controls.append(button);
			});
			const quantity = document.createElement("span");
			quantity.textContent = item.qty;
			controls.insertBefore(quantity, controls.lastChild);
			details.append(title, subtotal, controls);
			const remove = document.createElement("button");
			remove.type = "button";
			remove.className = "cart-remove";
			remove.textContent = "Remove";
			remove.dataset.cartChange = "0";
			remove.dataset.productId = item.product_id;
			row.append(details, remove);
			cartItems.append(row);
		});
	};

	const updatePayableTotal = () => {
		const requestedDiscount = coinToggle?.checked ? Number(currentCart.max_coin_discount || 0) : 0;
		const discount = Math.min(requestedDiscount, Number(currentCart.coin_balance || 0));
		const payable = Math.max(0, Number(currentCart.subtotal ?? currentCart.total ?? 0) - discount);
		const discountRow = cartDrawer.querySelector("[data-coin-discount-row]");
		if (discountRow) discountRow.hidden = discount <= 0;
		const discountOutput = cartDrawer.querySelector("[data-coin-discount]");
		if (discountOutput) discountOutput.textContent = money(discount);
		cartDrawer.querySelectorAll("[data-cart-pay-total], [data-cart-payable]").forEach((node) => { node.textContent = money(payable); });
		if (redeemCoinsInput) redeemCoinsInput.value = coinToggle?.checked && discount > 0 ? "1" : "";
	};

	const sendCart = async (url, body = {}) => {
		cartStatus.textContent = "";
		try {
			const response = await fetch(url, {
				method: "POST",
				credentials: "same-origin",
				headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken || "" },
				body: JSON.stringify(body)
			});
			const result = await response.json();
			if (!response.ok) throw new Error(result.error || "Cart could not be updated.");
			renderCart(result);
			return true;
		} catch (error) {
			cartStatus.textContent = error.message || "Cart could not be updated. Check your connection and try again.";
			return false;
		}
	};

	document.querySelector("[data-cart-open]")?.addEventListener("click", () => setDrawer(true));
	document.querySelectorAll("[data-cart-close]").forEach((button) => button.addEventListener("click", () => setDrawer(false)));
	document.addEventListener("keydown", (event) => { if (event.key === "Escape") setDrawer(false); });
	document.addEventListener("click", async (event) => {
		const addButton = event.target.closest("[data-cart-add]");
		if (addButton) {
			const added = await sendCart(cartDrawer.dataset.addUrl, { product_id: addButton.dataset.cartAdd, qty: 1 });
			if (added) setDrawer(true);
			return;
		}
		const changeButton = event.target.closest("[data-cart-change]");
		if (changeButton) {
			const row = changeButton.closest(".cart-item");
			const currentQuantity = Number(row.querySelector(".cart-quantity span").textContent);
			await sendCart(cartDrawer.dataset.updateUrl, {
				product_id: changeButton.dataset.productId,
				qty: changeButton.dataset.cartChange === "0" ? 0 : currentQuantity + Number(changeButton.dataset.cartChange)
			});
		}
	});
	cartDrawer.querySelector("[data-cart-clear]")?.addEventListener("click", () => sendCart(cartDrawer.dataset.clearUrl));
	coinToggle?.addEventListener("change", updatePayableTotal);
	fetch(cartDrawer.dataset.updateUrl, {
		method: "GET",
		credentials: "same-origin",
		headers: { "Accept": "application/json" }
	}).then((response) => {
		if (response.status === 405) return null;
		return response.json();
	}).then((cart) => { if (cart) renderCart(cart); }).catch(() => {});
}