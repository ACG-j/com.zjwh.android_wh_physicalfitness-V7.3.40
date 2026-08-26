import argparse
import json
import os
import sys

cur_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, cur_dir)

from sport_client import SportClient


def main():
    print("运动世界校园 / 室外跑")
    print()

    parser = argparse.ArgumentParser(description="提交室外跑记录")
    parser.add_argument("-t", "--token", help="登录 Token")
    parser.add_argument("-u", "--uid", help="用户 UID")
    parser.add_argument("--unid", default="0", help="学校 UNID（默认 0）")
    parser.add_argument("-d", "--distance", type=float, help="跑步距离，单位米")
    parser.add_argument("--time", type=int, help="跑步用时，单位秒")
    parser.add_argument("--steps", type=int, help="跑步步数")
    parser.add_argument("--type", type=int, default=1, choices=[1, 4], help="类型：1 计分跑，4 自由跑（默认 1）")
    parser.add_argument("--proxy", help="代理地址")
    args = parser.parse_args()

    token = args.token
    if not token:
        try:
            token = input("登录 Token: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n操作已取消")
            return

    if not token:
        print("错误: Token 不能为空")
        return

    uid = args.uid
    if not uid:
        try:
            uid = input("用户 UID: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n操作已取消")
            return

    if not uid:
        print("错误: UID 不能为空")
        return

    distance = args.distance
    if not distance:
        try:
            dist_str = input("跑步距离（米，默认 2000）: ").strip()
            distance = float(dist_str) if dist_str else 2000.0
        except (KeyboardInterrupt, EOFError):
            print("\n操作已取消")
            return
        except ValueError:
            distance = 2000.0

    duration = args.time
    if not duration:
        try:
            time_str = input("跑步用时（秒，默认 720）: ").strip()
            duration = int(time_str) if time_str else 720
        except (KeyboardInterrupt, EOFError):
            print("\n操作已取消")
            return
        except ValueError:
            duration = 720

    steps = args.steps
    if not steps:
        try:
            step_str = input("步数（默认按距离计算）: ").strip()
            steps = int(step_str) if step_str else int(distance * 1.05)
        except (KeyboardInterrupt, EOFError):
            print("\n操作已取消")
            return
        except ValueError:
            steps = int(distance * 1.05)

    pace_min = (duration / 60.0) / (distance / 1000.0)
    run_type = "计分跑" if args.type == 1 else "自由跑"
    print("\n本次跑步")
    print(f"类型: {run_type}")
    print(f"距离: {distance:.1f} 米")
    print(f"用时: {duration} 秒")
    print(f"步数: {steps}")
    print(f"配速: {pace_min:.2f} 分钟/公里")

    client = SportClient(uid=uid, token=token, unid=int(args.unid), proxy=args.proxy)

    print("\n正在生成并提交记录...")

    res = client.submit_outdoor_run(
        total_distance_m=distance,
        total_time_sec=duration,
        total_steps=steps,
        sport_type=args.type
    )

    if res.get("error") == 10000 or res.get("code") == 0:
        print("\n提交成功")
    else:
        print("\n提交失败")
    print(json.dumps(res, ensure_ascii=False, indent=2))
    print()


if __name__ == "__main__":
    main()
