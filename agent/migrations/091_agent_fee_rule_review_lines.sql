-- 业务手册 → 计算规则 → 核对 (2026-11-20, 方案 A).
--
-- Checking a fee rule is now done line by line on its 收费表 (shared/fee_rule_lines.py):
-- every amount is read out as one line and ticked against the manual page shown beside it;
-- ticks are kept in checks_json as {"kind":"line","sig":...}. What the manual leaves open is
-- a question with answer options ({"kind":"question",...}), answered in the back office.
--
-- The V6.1 first-batch drafts (migration 090) carried free-text items: "对照第 N 页核对"
-- is now the 收费表 itself and "执行日期…尚未填写" is the basic-info check, so those items
-- go; the three open points become questions. Only drafts whose items are still exactly as
-- seeded (nothing ticked or edited since) are rewritten; anything else is left as it is.

UPDATE agent_fee_rule_versions
SET checks_json='[{"kind":"question","text":"托/木箱有电梯上楼，一票里单件有的超过 150 公斤、有的不超过，手册没写明怎么收（第 51 页）。","options":["需人工判断（现在的做法）","其他（写明做法）"],"answer":null,"note":""},{"kind":"question","text":"一票里既有床垫又有非床垫：手册写分开计算取大值，两部分怎么划分没写明（第 52 页）。","options":["需人工判断（现在的做法）","其他（写明做法）"],"answer":null,"note":""}]', updated_by='migration:091', row_version=row_version+1
WHERE rule_id='29e79711-0f15-5421-9cbc-ec74d21d0e3d' AND status='DRAFT'
  AND checks_json=CAST('[{"done":false,"text":"基础费、有电梯收费、楼层加收、单件加收逐格对照手册第 50 页原表核对。"},{"done":false,"text":"托/木箱有电梯：单件有的超过 150 公斤、有的不超过时手册未写明，目前按“需人工判断”（第 51 页）。"},{"done":false,"text":"床垫与非床垫混在一票：分开计算取大，两部分怎么划分需人工判断（第 52 页）。"},{"done":false,"text":"床垫收费表（首件、续件、封顶 400 元）对照第 51–52 页核对。"},{"done":false,"text":"执行日期以总部通知为准，尚未填写。"}]' AS JSON);

UPDATE agent_fee_rule_versions
SET checks_json='[{"kind":"question","text":"卸货费按哪个重量算？手册表头写结算重量，第 48 页的例子用的是实际重量。","options":["结算重量（现在的做法）","实际重量（需在规则内容里改）","其他（写明做法）"],"answer":null,"note":""}]', updated_by='migration:091', row_version=row_version+1
WHERE rule_id='d13aeee8-1890-5ffd-9388-a2ee9a158673' AND status='DRAFT'
  AND checks_json=CAST('[{"done":false,"text":"单票 >1500kg 0.015 元/kg（最低 25、最高 300）、单件 >1500kg 50 元/件对照第 46–47 页核对。"},{"done":false,"text":"手册表头写结算重量，第 48 页例子用实际重量，填哪个请统一。"},{"done":false,"text":"执行日期以总部通知为准，尚未填写。"}]' AS JSON);

UPDATE agent_fee_rule_versions
SET checks_json='[]', updated_by='migration:091', row_version=row_version+1
WHERE rule_id='6784f9a5-26a3-5690-bdc1-f746be0dd4e8' AND status='DRAFT'
  AND checks_json=CAST('[{"done":false,"text":"拆托 2 件以下免费、3 件起 10 元每多一件 +5 元、拆木架木箱 10 元/件对照第 59 页核对。"},{"done":false,"text":"执行日期以总部通知为准，尚未填写。"}]' AS JSON);

UPDATE agent_fee_rule_versions
SET checks_json='[]', updated_by='migration:091', row_version=row_version+1
WHERE rule_id='d80b9eb5-3924-5235-8180-cd359822ff2e' AND status='DRAFT'
  AND checks_json=CAST('[{"done":false,"text":"20 件以内免费、超过 0.1 元/件（最低 5、最高 100）对照第 60 页核对。"},{"done":false,"text":"执行日期以总部通知为准，尚未填写。"}]' AS JSON);

UPDATE agent_fee_rule_versions
SET checks_json='[]', updated_by='migration:091', row_version=row_version+1
WHERE rule_id='5f0a7022-d012-5d47-9a01-4020a4a848e0' AND status='DRAFT'
  AND checks_json=CAST('[{"done":false,"text":"分类码放、码放上架、分房号码放三项金额与上下限对照第 61–62 页核对。"},{"done":false,"text":"执行日期以总部通知为准，尚未填写。"}]' AS JSON);

UPDATE agent_fee_rule_versions
SET checks_json='[]', updated_by='migration:091', row_version=row_version+1
WHERE rule_id='caa91aa3-1cd2-5233-8d73-667f2ca32fdd' AND status='DRAFT'
  AND checks_json=CAST('[{"done":false,"text":"夜送 50 元/票对照第 62 页核对。"},{"done":false,"text":"执行日期以总部通知为准，尚未填写。"}]' AS JSON);

UPDATE agent_fee_rule_versions
SET checks_json='[]', updated_by='migration:091', row_version=row_version+1
WHERE rule_id='ec892561-f140-5f72-9318-e2a6417adca2' AND status='DRAFT'
  AND checks_json=CAST('[{"done":false,"text":"N 为包含收货地址在内一共要去的地址数（用户 2026-11-19 确认），(N−1)×10 元、最低 10 元对照第 63 页核对。"},{"done":false,"text":"执行日期以总部通知为准，尚未填写。"}]' AS JSON);

UPDATE agent_fee_rule_versions
SET checks_json='[]', updated_by='migration:091', row_version=row_version+1
WHERE rule_id='ca87dc7e-f2e4-5129-a178-27fe59804626' AND status='DRAFT'
  AND checks_json=CAST('[{"done":false,"text":"二次上门 10 元/票对照第 63 页核对。"},{"done":false,"text":"执行日期以总部通知为准，尚未填写。"}]' AS JSON);

UPDATE agent_fee_rule_versions
SET checks_json='[]', updated_by='migration:091', row_version=row_version+1
WHERE rule_id='cf3e2be7-681b-5b6b-8f35-0f7407ad6ee7' AND status='DRAFT'
  AND checks_json=CAST('[{"done":false,"text":"托盘按拆托标准、非托盘 0.5 元/件（最低 10、最高 100）对照第 63 页核对。"},{"done":false,"text":"执行日期以总部通知为准，尚未填写。"}]' AS JSON);

UPDATE agent_fee_rule_versions
SET checks_json='[]', updated_by='migration:091', row_version=row_version+1
WHERE rule_id='f10d5910-7b0c-5f9c-b70f-04261e7c6f7a' AND status='DRAFT'
  AND checks_json=CAST('[{"done":false,"text":"第 3 次起每次加收（基础派费 + 10 元）对照第 63 页核对。"},{"done":false,"text":"执行日期以总部通知为准，尚未填写。"}]' AS JSON);

UPDATE agent_fee_rule_versions
SET checks_json='[]', updated_by='migration:091', row_version=row_version+1
WHERE rule_id='18480b0a-00db-58c7-b785-65381d8a3cc6' AND status='DRAFT'
  AND checks_json=CAST('[{"done":false,"text":"过路费凭发票实报实销对照第 63 页核对。"},{"done":false,"text":"执行日期以总部通知为准，尚未填写。"}]' AS JSON);
