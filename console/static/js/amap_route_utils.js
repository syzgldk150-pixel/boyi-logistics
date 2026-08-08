(function () {
  function normalizePosition(location) {
    if (!location) return null;
    if (Array.isArray(location) && location.length >= 2) {
      const lng = Number(location[0]);
      const lat = Number(location[1]);
      return Number.isFinite(lng) && Number.isFinite(lat) ? [lng, lat] : null;
    }
    const lng = Number(location.lng ?? location.getLng?.());
    const lat = Number(location.lat ?? location.getLat?.());
    return Number.isFinite(lng) && Number.isFinite(lat) ? [lng, lat] : null;
  }

  function formatDistance(distanceMeters) {
    return (Math.max(0, Number(distanceMeters) || 0) / 1000).toFixed(1);
  }

  function formatDuration(durationSeconds) {
    const totalMinutes = Math.max(1, Math.round((Number(durationSeconds) || 0) / 60));
    const hours = Math.floor(totalMinutes / 60);
    const minutes = totalMinutes % 60;
    if (hours && minutes) return `预计 ${hours} 小时 ${minutes} 分`;
    if (hours) return `预计 ${hours} 小时`;
    return `预计 ${totalMinutes} 分`;
  }

  function createDrivingService(AMap, map, options = {}) {
    if (!AMap?.Driving || !map) return null;
    return new AMap.Driving({
      map,
      showTraffic: options.showTraffic ?? true,
      autoFitView: options.autoFitView ?? true,
      hideMarkers: options.hideMarkers ?? false,
      policy: options.policy ?? AMap.DrivingPolicy?.LEAST_TIME,
    });
  }

  function searchDrivingRoute(driving, originPoint, destPoint, messages = {}) {
    return new Promise((resolve, reject) => {
      if (!driving) {
        reject(new Error(messages.serviceMissing || "驾车规划服务未就绪"));
        return;
      }
      if (typeof driving.clear === "function") driving.clear();
      driving.search(originPoint, destPoint, {}, (status, result) => {
        if (status === "complete" && result?.routes?.length) {
          resolve(result);
          return;
        }
        if (status === "no_data") {
          reject(new Error(messages.noData || "未找到可规划的驾车路线"));
          return;
        }
        reject(new Error(messages.failure || "驾车路线规划失败"));
      });
    });
  }

  window.AMapRouteUtils = {
    normalizePosition,
    formatDistance,
    formatDuration,
    createDrivingService,
    searchDrivingRoute,
  };
})();
