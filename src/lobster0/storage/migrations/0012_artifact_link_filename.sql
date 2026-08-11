-- 附件的显示文件名。
-- 不放在 artifacts 表上：那张表是内容寻址且跨会话去重的，同一份内容用两个名字
-- 上传应该保留两个名字，所以文件名属于「每次出现」，也就是 artifact_links。
ALTER TABLE artifact_links ADD COLUMN filename TEXT;
